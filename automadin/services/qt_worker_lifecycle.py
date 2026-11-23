"""Small, conservative helpers for ambiguous ``QThread.start()`` failures."""

from __future__ import annotations

from typing import Any


def qthread_start_may_have_succeeded(worker: Any | None) -> bool:
    """Return whether a worker is running or has already finished.

    Qt can report a startup exception after the native thread has begun.  A
    short worker may even finish before the exception reaches its caller, so
    ``isRunning()`` alone is not sufficient.  If state inspection itself
    fails, retain the worker conservatively instead of risking destruction of
    a live native thread.
    """
    if worker is None:
        return False
    for method_name in ("isRunning", "isFinished"):
        method = getattr(worker, method_name, None)
        if not callable(method):
            continue
        try:
            state = method()
        except BaseException:
            return True
        if isinstance(state, bool) and state:
            return True
    return False
