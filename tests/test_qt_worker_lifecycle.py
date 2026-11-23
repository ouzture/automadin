from __future__ import annotations

from automadin.services.qt_worker_lifecycle import (
    qthread_start_may_have_succeeded,
)


class _WorkerState:
    def __init__(self, *, running: bool = False, finished: bool = False) -> None:
        self._running = running
        self._finished = finished

    def isRunning(self) -> bool:
        return self._running

    def isFinished(self) -> bool:
        return self._finished


def test_worker_that_finished_before_start_error_is_treated_as_started() -> None:
    assert qthread_start_may_have_succeeded(_WorkerState(finished=True))


def test_never_started_worker_is_safe_to_roll_back() -> None:
    assert not qthread_start_may_have_succeeded(_WorkerState())
    assert not qthread_start_may_have_succeeded(None)


def test_state_inspection_failure_is_conservatively_retained() -> None:
    class _UncertainWorker:
        @staticmethod
        def isRunning() -> bool:
            raise RuntimeError("state unavailable")

    assert qthread_start_may_have_succeeded(_UncertainWorker())
