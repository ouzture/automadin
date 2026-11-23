from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from automadin.models.schedule import (
    RunHistoryOutcome,
    RunHistoryRecord,
    ScheduleSpec,
    ScheduledFlow,
)

if TYPE_CHECKING:
    from automadin.services.database import DatabaseService


_MAX_NAME_LENGTH = 160
_MAX_HISTORY_MESSAGE_LENGTH = 1000
_HISTORY_RETENTION_DAYS = 90
_MAX_HISTORY_RECORDS = 5000
_HISTORY_PRUNE_BATCH = 200


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Schedule timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _parse_utc(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("Stored schedule timestamp is invalid")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Stored schedule timestamp is not timezone-aware")
    return parsed.astimezone(UTC)


def _validated_name(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    name = value.strip()
    if (
        not name
        or len(name) > _MAX_NAME_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise ValueError(
            f"{label} must contain 1 to {_MAX_NAME_LENGTH} printable characters"
        )
    return name


def _canonical_uuid(value: object, label: str) -> str:
    try:
        canonical = str(uuid.UUID(str(value)))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{label} must be a UUID") from exc
    if str(value) != canonical:
        raise ValueError(f"{label} must be a canonical UUID")
    return canonical


class ScheduleRepository:
    """Persistence boundary for schedules, atomic claims, and run history."""

    def __init__(self, database: DatabaseService) -> None:
        self._database = database

    @property
    def _connection(self) -> sqlite3.Connection:
        return self._database._conn

    def create(
        self,
        *,
        name: str,
        run_config_id: int,
        spec: ScheduleSpec,
        enabled: bool = True,
        next_due_at_utc: datetime | None = None,
        schedule_id: str | None = None,
        now_utc: datetime | None = None,
    ) -> ScheduledFlow:
        identity = str(uuid.uuid4()) if schedule_id is None else _canonical_uuid(
            schedule_id, "schedule_id"
        )
        clean_name = _validated_name(name, "Schedule name")
        if type(run_config_id) is not int or run_config_id < 1:
            raise ValueError("run_config_id must be a positive integer")
        now_text = _utc_text(now_utc or _utc_now())
        try:
            self._connection.execute(
                """
                INSERT INTO schedules (
                    schedule_id, name, run_config_id, data, enabled,
                    next_due_at_utc, last_due_at_utc, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    identity,
                    clean_name,
                    run_config_id,
                    json.dumps(spec.to_dict()),
                    int(bool(enabled)),
                    _utc_text(next_due_at_utc),
                    now_text,
                    now_text,
                ),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as exc:
            self._connection.rollback()
            raise ValueError(
                "Schedule name already exists or its run setup is unavailable"
            ) from exc
        created = self.get(identity)
        if created is None:  # pragma: no cover - guarded by successful insert
            raise RuntimeError("Created schedule could not be loaded")
        return created

    def get(self, schedule_id: str) -> ScheduledFlow | None:
        identity = _canonical_uuid(schedule_id, "schedule_id")
        row = self._connection.execute(
            "SELECT * FROM schedules WHERE schedule_id = ?",
            (identity,),
        ).fetchone()
        return self._schedule_from_row(row) if row is not None else None

    def list(self) -> list[ScheduledFlow]:
        rows = self._connection.execute(
            "SELECT * FROM schedules ORDER BY name COLLATE NOCASE, schedule_id"
        ).fetchall()
        return [self._schedule_from_row(row) for row in rows]

    def list_due(self, at_utc: datetime, *, limit: int = 100) -> list[ScheduledFlow]:
        bounded_limit = max(1, min(int(limit), 1000))
        rows = self._connection.execute(
            """
            SELECT * FROM schedules
             WHERE enabled = 1
               AND next_due_at_utc IS NOT NULL
               AND next_due_at_utc <= ?
             ORDER BY next_due_at_utc, schedule_id
             LIMIT ?
            """,
            (_utc_text(at_utc), bounded_limit),
        ).fetchall()
        return [self._schedule_from_row(row) for row in rows]

    def next_enabled_due(self) -> datetime | None:
        row = self._connection.execute(
            """
            SELECT MIN(next_due_at_utc) AS next_due
              FROM schedules
             WHERE enabled = 1 AND next_due_at_utc IS NOT NULL
            """
        ).fetchone()
        return _parse_utc(row["next_due"]) if row is not None else None

    def next_enabled(self) -> ScheduledFlow | None:
        row = self._connection.execute(
            """
            SELECT * FROM schedules
             WHERE enabled = 1 AND next_due_at_utc IS NOT NULL
             ORDER BY next_due_at_utc, schedule_id
             LIMIT 1
            """
        ).fetchone()
        return self._schedule_from_row(row) if row is not None else None

    def enabled_count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM schedules WHERE enabled = 1"
        ).fetchone()
        return int(row["count"]) if row is not None else 0

    def set_next_due(
        self,
        schedule_id: str,
        next_due_at_utc: datetime | None,
        *,
        now_utc: datetime | None = None,
    ) -> bool:
        identity = _canonical_uuid(schedule_id, "schedule_id")
        cursor = self._connection.execute(
            """
            UPDATE schedules
               SET next_due_at_utc = ?, updated_at = ?
             WHERE schedule_id = ?
            """,
            (
                _utc_text(next_due_at_utc),
                _utc_text(now_utc or _utc_now()),
                identity,
            ),
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def recover_incomplete_runs(
        self,
        *,
        finished_at_utc: datetime | None = None,
    ) -> int:
        cursor = self._connection.execute(
            """
            UPDATE run_history
               SET outcome = 'failed',
                   finished_at_utc = ?,
                   message = ?
             WHERE source = 'scheduled'
               AND outcome IN ('pending', 'running')
            """,
            (
                _utc_text(finished_at_utc or _utc_now()),
                "Automadin stopped before this scheduled run reported completion.",
            ),
        )
        self._connection.commit()
        return cursor.rowcount

    def update(
        self,
        schedule_id: str,
        *,
        name: str,
        run_config_id: int,
        spec: ScheduleSpec,
        enabled: bool,
        next_due_at_utc: datetime | None,
        now_utc: datetime | None = None,
    ) -> bool:
        identity = _canonical_uuid(schedule_id, "schedule_id")
        clean_name = _validated_name(name, "Schedule name")
        if type(run_config_id) is not int or run_config_id < 1:
            raise ValueError("run_config_id must be a positive integer")
        try:
            cursor = self._connection.execute(
                """
                UPDATE schedules
                   SET name = ?, run_config_id = ?, data = ?, enabled = ?,
                       next_due_at_utc = ?, updated_at = ?
                 WHERE schedule_id = ?
                """,
                (
                    clean_name,
                    run_config_id,
                    json.dumps(spec.to_dict()),
                    int(bool(enabled)),
                    _utc_text(next_due_at_utc),
                    _utc_text(now_utc or _utc_now()),
                    identity,
                ),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as exc:
            self._connection.rollback()
            raise ValueError(
                "Schedule name already exists or its run setup is unavailable"
            ) from exc
        return cursor.rowcount > 0

    def delete(self, schedule_id: str) -> bool:
        identity = _canonical_uuid(schedule_id, "schedule_id")
        cursor = self._connection.execute(
            "DELETE FROM schedules WHERE schedule_id = ?",
            (identity,),
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def claim_occurrence(
        self,
        schedule_id: str,
        *,
        expected_due_at_utc: datetime,
        next_due_at_utc: datetime | None,
        run_id: str | None = None,
        now_utc: datetime | None = None,
    ) -> RunHistoryRecord | None:
        """Advance one exact due occurrence and create its pending history row."""
        identity = _canonical_uuid(schedule_id, "schedule_id")
        expected_text = _utc_text(expected_due_at_utc)
        run_identity = str(uuid.uuid4()) if run_id is None else _canonical_uuid(
            run_id, "run_id"
        )
        now_text = _utc_text(now_utc or _utc_now())
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT s.name AS schedule_name, s.run_config_id,
                       r.name AS run_config_name, r.data AS run_config_data
                  FROM schedules AS s
                  JOIN run_configs AS r ON r.id = s.run_config_id
                 WHERE s.schedule_id = ?
                   AND s.enabled = 1
                   AND s.next_due_at_utc = ?
                """,
                (identity, expected_text),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            run_config_data = json.loads(row["run_config_data"])
            flow_name = _validated_name(
                run_config_data.get("flow_name"),
                "Flow name",
            )
            connection.execute(
                """
                UPDATE schedules
                   SET next_due_at_utc = ?, last_due_at_utc = ?, updated_at = ?
                 WHERE schedule_id = ?
                """,
                (
                    _utc_text(next_due_at_utc),
                    expected_text,
                    now_text,
                    identity,
                ),
            )
            connection.execute(
                """
                INSERT INTO run_history (
                    run_id, source, schedule_id, schedule_name, run_config_id,
                    run_config_name, flow_name, scheduled_for_utc,
                    started_at_utc, finished_at_utc, outcome, message
                )
                VALUES (?, 'scheduled', ?, ?, ?, ?, ?, ?, NULL, NULL, 'pending', '')
                """,
                (
                    run_identity,
                    identity,
                    row["schedule_name"],
                    row["run_config_id"],
                    row["run_config_name"],
                    flow_name,
                    expected_text,
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return self.get_history(run_identity)

    def mark_running(
        self,
        run_id: str,
        *,
        started_at_utc: datetime | None = None,
    ) -> bool:
        return self._update_history(
            run_id,
            outcome=RunHistoryOutcome.RUNNING,
            started_at_utc=started_at_utc or _utc_now(),
        )

    def finish(
        self,
        run_id: str,
        *,
        outcome: RunHistoryOutcome,
        message: str = "",
        finished_at_utc: datetime | None = None,
    ) -> bool:
        if outcome in (RunHistoryOutcome.PENDING, RunHistoryOutcome.RUNNING):
            raise ValueError("finish requires a terminal run outcome")
        return self._update_history(
            run_id,
            outcome=outcome,
            finished_at_utc=finished_at_utc or _utc_now(),
            message=message,
        )

    def _update_history(
        self,
        run_id: str,
        *,
        outcome: RunHistoryOutcome,
        started_at_utc: datetime | None = None,
        finished_at_utc: datetime | None = None,
        message: str | None = None,
    ) -> bool:
        identity = _canonical_uuid(run_id, "run_id")
        values: list[object] = [outcome.value]
        assignments = ["outcome = ?"]
        if started_at_utc is not None:
            assignments.append("started_at_utc = ?")
            values.append(_utc_text(started_at_utc))
        if finished_at_utc is not None:
            assignments.append("finished_at_utc = ?")
            values.append(_utc_text(finished_at_utc))
        if message is not None:
            assignments.append("message = ?")
            values.append(str(message)[:_MAX_HISTORY_MESSAGE_LENGTH])
        values.append(identity)
        cursor = self._connection.execute(
            f"UPDATE run_history SET {', '.join(assignments)} WHERE run_id = ?",
            values,
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def get_history(self, run_id: str) -> RunHistoryRecord | None:
        identity = _canonical_uuid(run_id, "run_id")
        row = self._connection.execute(
            "SELECT * FROM run_history WHERE run_id = ?",
            (identity,),
        ).fetchone()
        return self._history_from_row(row) if row is not None else None

    def list_history(self, *, limit: int = 100) -> list[RunHistoryRecord]:
        bounded_limit = max(1, min(int(limit), 5000))
        rows = self._connection.execute(
            """
            SELECT * FROM run_history
             ORDER BY scheduled_for_utc DESC, run_id DESC
             LIMIT ?
            """,
            (bounded_limit,),
        ).fetchall()
        return [self._history_from_row(row) for row in rows]

    def prune_history(
        self,
        *,
        now_utc: datetime | None = None,
        retention_days: int = _HISTORY_RETENTION_DAYS,
        max_records: int = _MAX_HISTORY_RECORDS,
        batch_size: int = _HISTORY_PRUNE_BATCH,
    ) -> int:
        """Delete one bounded batch of old terminal history records."""
        if retention_days < 1 or max_records < 1 or batch_size < 1:
            raise ValueError("History retention limits must be positive")
        connection = self._connection
        cutoff = (now_utc or _utc_now()) - timedelta(days=retention_days)
        deleted = 0
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                DELETE FROM run_history
                 WHERE run_id IN (
                    SELECT run_id
                      FROM run_history
                     WHERE outcome NOT IN ('pending', 'running')
                       AND finished_at_utc < ?
                     ORDER BY finished_at_utc, scheduled_for_utc, run_id
                     LIMIT ?
                 )
                """,
                (_utc_text(cutoff), batch_size),
            )
            deleted += cursor.rowcount
            remaining_batch = batch_size - deleted
            total_row = connection.execute(
                "SELECT COUNT(*) AS count FROM run_history"
            ).fetchone()
            excess = max(0, int(total_row["count"]) - max_records)
            if excess and remaining_batch:
                cursor = connection.execute(
                    """
                    DELETE FROM run_history
                     WHERE run_id IN (
                        SELECT run_id
                          FROM run_history
                         WHERE outcome NOT IN ('pending', 'running')
                         ORDER BY scheduled_for_utc, run_id
                         LIMIT ?
                     )
                    """,
                    (min(excess, remaining_batch),),
                )
                deleted += cursor.rowcount
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return deleted

    @staticmethod
    def _schedule_from_row(row: sqlite3.Row) -> ScheduledFlow:
        return ScheduledFlow(
            schedule_id=_canonical_uuid(row["schedule_id"], "schedule_id"),
            name=_validated_name(row["name"], "Schedule name"),
            run_config_id=int(row["run_config_id"]),
            spec=ScheduleSpec.from_dict(json.loads(row["data"])),
            enabled=bool(row["enabled"]),
            next_due_at_utc=_parse_utc(row["next_due_at_utc"]),
            last_due_at_utc=_parse_utc(row["last_due_at_utc"]),
            created_at_utc=_parse_utc(row["created_at"]),
            updated_at_utc=_parse_utc(row["updated_at"]),
        )

    @staticmethod
    def _history_from_row(row: sqlite3.Row) -> RunHistoryRecord:
        scheduled_for = _parse_utc(row["scheduled_for_utc"])
        if scheduled_for is None:  # pragma: no cover - NOT NULL schema
            raise ValueError("Stored history occurrence is missing")
        return RunHistoryRecord(
            run_id=_canonical_uuid(row["run_id"], "run_id"),
            source=str(row["source"]),
            schedule_id=(
                _canonical_uuid(row["schedule_id"], "schedule_id")
                if row["schedule_id"] is not None
                else None
            ),
            schedule_name=str(row["schedule_name"]),
            run_config_id=(
                int(row["run_config_id"])
                if row["run_config_id"] is not None
                else None
            ),
            run_config_name=str(row["run_config_name"]),
            flow_name=str(row["flow_name"]),
            scheduled_for_utc=scheduled_for,
            started_at_utc=_parse_utc(row["started_at_utc"]),
            finished_at_utc=_parse_utc(row["finished_at_utc"]),
            outcome=RunHistoryOutcome(row["outcome"]),
            message=str(row["message"]),
        )
