"""Live execution-log detail levels shared by runtime and UI surfaces."""

from __future__ import annotations

from enum import IntEnum


class LogGranularity(IntEnum):
    NORMAL = 0
    DETAILED = 1
    TRACE = 2


__all__ = ["LogGranularity"]
