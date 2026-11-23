from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, timezone
from types import SimpleNamespace

from automadin.execution.run_coordinator import RunCapacityError
from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.models.device import make_windows_saved_device_id
from automadin.models.run_config import RunStatus, SavedRunConfig
from automadin.models.schedule import (
    MissedRunPolicy,
    OverlapPolicy,
    RunHistoryOutcome,
    SchedulePattern,
    ScheduleSpec,
)
from automadin.services.database import DatabaseService
from automadin.services.schedule_repository import ScheduleRepository
from automadin.services.scheduler_dispatcher import ScheduleDispatcher


class _Signal:
    def __init__(self) -> None:
        self._callbacks = []

    def connect(self, callback) -> None:
        self._callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in tuple(self._callbacks):
            callback(*args)


class _Worker:
    def __init__(self, *, auto_finish: bool) -> None:
        self.status_changed = _Signal()
        self.completed = _Signal()
        self.finished = _Signal()
        self.status = RunStatus.IDLE
        self.auto_finish = auto_finish
        self.attention_required_message: str | None = None
        self.ctx = SimpleNamespace(stop=lambda: None)

    def finish(self, status: RunStatus = RunStatus.COMPLETED) -> None:
        self.status = status
        self.status_changed.emit(status.value)
        self.completed.emit("scheduled result")
        self.finished.emit()


class _Coordinator:
    def __init__(self, *, auto_finish: bool = True) -> None:
        self.auto_finish = auto_finish
        self.capacity_blocked = False
        self.workers: dict[str, _Worker] = {}
        self.removed: list[str] = []
        self.scheduled_prepare_calls = 0
        self.scheduled_preflight_error = False

    def prepare(self, saved_config):
        errors = (
            (SimpleNamespace(message="Windows UI automation is blocked."),)
            if self.scheduled_preflight_error
            else ()
        )
        return SimpleNamespace(
            saved_config=saved_config,
            preflight=SimpleNamespace(
                errors=errors,
                warnings=(),
            ),
        )

    def prepare_scheduled(self, saved_config):
        self.scheduled_prepare_calls += 1
        return self.prepare(saved_config)

    def register(self, prepared, *, run_id: str):
        if self.capacity_blocked:
            raise RunCapacityError("full")
        worker = _Worker(auto_finish=self.auto_finish)
        self.workers[run_id] = worker
        return SimpleNamespace(
            run_id=run_id,
            prepared=prepared,
            worker=worker,
        )

    def start(self, registered) -> None:
        worker = registered.worker
        worker.status = RunStatus.RUNNING
        worker.status_changed.emit(RunStatus.RUNNING.value)
        if worker.auto_finish:
            worker.finish()

    def remove(self, run_id: str) -> bool:
        self.removed.append(run_id)
        self.workers.pop(run_id, None)
        return True


def _setup(
    tmp_path,
    *,
    spec: ScheduleSpec,
    due: datetime,
    auto_finish: bool = True,
    command_id: str = "wait",
    device_id: str = "emulator-5554",
):
    database = DatabaseService(str(tmp_path / "scheduler.db"))
    params = {"ms": 1} if command_id == "wait" else {}
    flow = AutomationFlow("Scheduled flow", [FlowCommand(command_id, params=params)])
    database.save_flow(flow.name, flow)
    run_setup = SavedRunConfig(
        name="Scheduled setup",
        flow_name=flow.name,
        device_id=device_id,
    )
    database.save_run_config(run_setup.name, run_setup)
    run_config_id = database.get_run_config_id(run_setup.name)
    assert run_config_id is not None
    repository = ScheduleRepository(database)
    schedule = repository.create(
        name="Schedule",
        run_config_id=run_config_id,
        spec=spec,
        next_due_at_utc=due,
    )
    coordinator = _Coordinator(auto_finish=auto_finish)
    dispatcher = ScheduleDispatcher(
        database,
        repository,
        coordinator,
        local_timezone=UTC,
        clock=lambda: due,
    )
    return database, repository, schedule, coordinator, dispatcher


def test_due_occurrence_runs_once_and_records_completion(tmp_path) -> None:
    due = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
    database, repository, schedule, coordinator, dispatcher = _setup(
        tmp_path,
        spec=ScheduleSpec(pattern=SchedulePattern.DAILY, local_time=time(9, 0)),
        due=due,
    )

    dispatcher.tick(due)

    history = repository.list_history()
    assert len(history) == 1
    assert history[0].outcome is RunHistoryOutcome.COMPLETED
    assert history[0].message == "scheduled result"
    assert coordinator.removed == [history[0].run_id]
    assert repository.get(schedule.schedule_id).next_due_at_utc == due + timedelta(days=1)
    assert coordinator.scheduled_prepare_calls == 1
    database.close()


def test_scheduled_preparation_block_is_persisted_without_starting_worker(
    tmp_path,
) -> None:
    due = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
    database, repository, _schedule, coordinator, dispatcher = _setup(
        tmp_path,
        spec=ScheduleSpec(pattern=SchedulePattern.DAILY, local_time=time(9, 0)),
        due=due,
    )
    coordinator.scheduled_preflight_error = True

    dispatcher.tick(due)

    history = repository.list_history()[0]
    assert history.outcome is RunHistoryOutcome.BLOCKED
    assert history.message == (
        "Scheduled preflight needs attention: Windows UI automation is blocked."
    )
    assert coordinator.scheduled_prepare_calls == 1
    assert not coordinator.workers
    database.close()


def test_locked_windows_interactive_block_is_persisted_in_history(tmp_path) -> None:
    due = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
    database, repository, _schedule, coordinator, dispatcher = _setup(
        tmp_path,
        spec=ScheduleSpec(pattern=SchedulePattern.DAILY, local_time=time(9, 0)),
        due=due,
        command_id="win_focus",
        device_id=make_windows_saved_device_id(123, "Calculator"),
    )
    coordinator.scheduled_preflight_error = True

    dispatcher.tick(due)

    history = repository.list_history()[0]
    assert history.outcome is RunHistoryOutcome.BLOCKED
    assert not coordinator.workers
    database.close()


def test_unattended_attention_checkpoint_records_a_distinct_history_outcome(tmp_path) -> None:
    due = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
    database, repository, _schedule, coordinator, dispatcher = _setup(
        tmp_path,
        spec=ScheduleSpec(pattern=SchedulePattern.DAILY, local_time=time(9, 0)),
        due=due,
        auto_finish=False,
    )

    dispatcher.tick(due)
    history = repository.list_history()[0]
    worker = coordinator.workers[history.run_id]
    worker.attention_required_message = "Open the account page and sign in."
    worker.finish(RunStatus.STOPPED)

    recorded = repository.get_history(history.run_id)
    assert recorded is not None
    assert recorded.outcome is RunHistoryOutcome.REQUIRES_ATTENTION
    assert recorded.message == "Requires attention: Open the account page and sign in."
    database.close()


def test_missed_skip_does_not_start_a_worker(tmp_path) -> None:
    due = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
    database, repository, _schedule, coordinator, dispatcher = _setup(
        tmp_path,
        spec=ScheduleSpec(
            pattern=SchedulePattern.DAILY,
            local_time=time(9, 0),
            missed_policy=MissedRunPolicy.SKIP,
        ),
        due=due,
    )

    dispatcher.tick(due + timedelta(minutes=5))

    assert repository.list_history()[0].outcome is RunHistoryOutcome.SKIPPED
    assert not coordinator.workers
    database.close()


def test_missed_run_once_starts_only_one_current_occurrence(tmp_path) -> None:
    due = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
    now = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)
    database, repository, schedule, _coordinator, dispatcher = _setup(
        tmp_path,
        spec=ScheduleSpec(
            pattern=SchedulePattern.DAILY,
            local_time=time(9, 0),
            missed_policy=MissedRunPolicy.RUN_ONCE,
        ),
        due=due,
    )

    dispatcher.tick(now)

    assert repository.list_history()[0].outcome is RunHistoryOutcome.COMPLETED
    assert repository.get(schedule.schedule_id).next_due_at_utc == datetime(
        2026, 7, 26, 9, 0, tzinfo=UTC
    )
    database.close()


def test_overlap_skip_does_not_create_a_second_worker(tmp_path) -> None:
    due = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
    database, repository, _schedule, coordinator, dispatcher = _setup(
        tmp_path,
        spec=ScheduleSpec(
            pattern=SchedulePattern.INTERVAL,
            start_at_local=datetime(2026, 7, 25, 9, 0),
            interval_minutes=1,
            overlap_policy=OverlapPolicy.SKIP,
        ),
        due=due,
        auto_finish=False,
    )
    dispatcher.tick(due)
    first_run_id = repository.list_history()[0].run_id

    dispatcher.tick(due + timedelta(minutes=1))

    history = repository.list_history()
    assert {record.outcome for record in history} == {
        RunHistoryOutcome.RUNNING,
        RunHistoryOutcome.SKIPPED,
    }
    assert list(coordinator.workers) == [first_run_id]
    database.close()


def test_queue_one_waits_for_active_occurrence_then_starts(tmp_path) -> None:
    due = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
    database, repository, _schedule, coordinator, dispatcher = _setup(
        tmp_path,
        spec=ScheduleSpec(
            pattern=SchedulePattern.INTERVAL,
            start_at_local=datetime(2026, 7, 25, 9, 0),
            interval_minutes=1,
            overlap_policy=OverlapPolicy.QUEUE_ONE,
        ),
        due=due,
        auto_finish=False,
    )
    dispatcher.tick(due)
    first = repository.list_history()[0]
    dispatcher.tick(due + timedelta(minutes=1))
    queued = repository.list_history()[0]
    assert queued.outcome is RunHistoryOutcome.PENDING

    coordinator.workers[first.run_id].finish()
    dispatcher.tick(due + timedelta(minutes=1, seconds=5))

    assert repository.get_history(queued.run_id).outcome is RunHistoryOutcome.RUNNING
    assert queued.run_id in coordinator.workers
    database.close()


def test_capacity_wait_is_bounded_to_fifteen_minutes(tmp_path) -> None:
    due = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
    database, repository, _schedule, coordinator, dispatcher = _setup(
        tmp_path,
        spec=ScheduleSpec(pattern=SchedulePattern.DAILY, local_time=time(9, 0)),
        due=due,
    )
    coordinator.capacity_blocked = True

    dispatcher.tick(due)
    dispatcher.tick(due + timedelta(minutes=15))

    history = repository.list_history()[0]
    assert history.outcome is RunHistoryOutcome.BLOCKED
    assert "15 minutes" in history.message
    database.close()


def test_capacity_retries_do_not_extend_the_original_deadline(tmp_path) -> None:
    due = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
    database, repository, _schedule, coordinator, dispatcher = _setup(
        tmp_path,
        spec=ScheduleSpec(pattern=SchedulePattern.DAILY, local_time=time(9, 0)),
        due=due,
    )
    coordinator.capacity_blocked = True

    dispatcher.tick(due)
    for minutes in range(1, 15):
        dispatcher.tick(due + timedelta(minutes=minutes))
    dispatcher.tick(due + timedelta(minutes=15))

    history = repository.list_history()[0]
    assert history.outcome is RunHistoryOutcome.BLOCKED
    assert "15 minutes" in history.message
    database.close()


def test_pause_holds_pending_runs_until_resumed(tmp_path) -> None:
    due = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
    database, repository, _schedule, coordinator, dispatcher = _setup(
        tmp_path,
        spec=ScheduleSpec(pattern=SchedulePattern.DAILY, local_time=time(9, 0)),
        due=due,
    )
    coordinator.capacity_blocked = True
    dispatcher.tick(due)

    coordinator.capacity_blocked = False
    dispatcher.set_paused(True)
    dispatcher.tick(due + timedelta(seconds=5))

    history = repository.list_history()[0]
    assert history.outcome is RunHistoryOutcome.PENDING
    assert not coordinator.workers

    dispatcher.set_paused(False)
    dispatcher.tick(due + timedelta(seconds=10))

    assert repository.get_history(history.run_id).outcome is RunHistoryOutcome.COMPLETED
    database.close()


def test_startup_recovers_interrupted_history(tmp_path) -> None:
    due = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)
    database, repository, schedule, _coordinator, dispatcher = _setup(
        tmp_path,
        spec=ScheduleSpec(pattern=SchedulePattern.DAILY, local_time=time(9, 0)),
        due=due,
        auto_finish=False,
    )
    claimed = repository.claim_occurrence(
        schedule.schedule_id,
        expected_due_at_utc=due,
        next_due_at_utc=due + timedelta(days=1),
    )
    assert claimed is not None

    recovered = dispatcher.start()

    assert recovered == 1
    assert repository.get_history(claimed.run_id).outcome is RunHistoryOutcome.FAILED
    database.close()


def test_timezone_change_recalculates_enabled_local_schedule(tmp_path) -> None:
    due = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
    database, repository, schedule, _coordinator, dispatcher = _setup(
        tmp_path,
        spec=ScheduleSpec(pattern=SchedulePattern.DAILY, local_time=time(9, 0)),
        due=due,
    )

    updated = dispatcher.update_local_timezone(
        timezone(timedelta(hours=2)),
        now_utc=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
    )

    assert updated == 1
    assert repository.get(schedule.schedule_id).next_due_at_utc == datetime(
        2026, 7, 26, 7, 0, tzinfo=UTC
    )
    database.close()
