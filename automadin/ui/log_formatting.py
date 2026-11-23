"""Semantic formatting for copyable live execution logs."""

from __future__ import annotations

import re
from functools import lru_cache

from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QTextEdit

from automadin.execution.handlers.registry import load_command_types
from automadin.ui.theme import CATEGORY_COLORS, COLORS

_TIMESTAMP_RE = re.compile(r"^(\[\d{2}:\d{2}:\d{2}(?:\.\d{3})?\])\s*")
_COMMAND_RE = re.compile(r"^(\[[0-9a-fA-F]{4}\])\s+([A-Za-z_][\w]*)(.*)$")
_LINK_RE = re.compile(r"(https?://\S+|\[[A-Za-z0-9_./,+ -]+\])")
_MILLISECONDS_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*ms\b", re.IGNORECASE)


def _compact_number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def format_duration_ms(milliseconds: int | float) -> str:
    """Format milliseconds for human-readable execution logs."""
    total_ms = max(0.0, float(milliseconds))
    if total_ms < 1000:
        return f"{_compact_number(total_ms)} ms"

    total_seconds = total_ms / 1000.0
    hours = int(total_seconds // 3600)
    remaining_seconds = total_seconds - hours * 3600
    minutes = int(remaining_seconds // 60)
    seconds = remaining_seconds - minutes * 60

    parts: list[str] = []
    if hours:
        parts.append(f"{hours} hr")
    if minutes:
        parts.append(f"{minutes} min")
    if seconds or not parts:
        parts.append(f"{_compact_number(seconds)} sec")
    return " ".join(parts)


def humanize_log_durations(message: str) -> str:
    """Replace millisecond literals in a log line with readable durations."""
    return _MILLISECONDS_RE.sub(
        lambda match: format_duration_ms(float(match.group(1))),
        message,
    )


@lru_cache(maxsize=1)
def _command_definitions() -> dict[str, dict[str, object]]:
    return load_command_types()


def command_label(command_id: str) -> str:
    """Return a concise user-facing command name for execution logs."""
    definition = _command_definitions().get(command_id, {})
    return str(definition.get("label", command_id.replace("_", " ").title()))


def _command_categories() -> dict[str, str]:
    return {
        command_id: str(definition.get("category", "ACTIONS"))
        for command_id, definition in _command_definitions().items()
    }


def command_color(command_id: str) -> str:
    category = _command_categories().get(command_id, "ACTIONS")
    return CATEGORY_COLORS.get(category, COLORS["primary"])


def message_color(message: str, level: str = "info") -> str:
    lowered = message.casefold()
    if level == "error" or any(
        word in lowered
        for word in (
            "error",
            "failed",
            "exception",
            "not met",
            "no match",
            "matched=no",
        )
    ):
        return COLORS["error"]
    if level == "complete" or any(
        word in lowered
        for word in (
            "completed",
            "success",
            "clicked",
            "opened",
            "closed",
            "current tab",
            "image match:",
            "condition 'found' met",
            "condition 'not_found' met",
            "matched=yes",
        )
    ):
        return COLORS["success"]
    if level == "warn" or any(
        word in lowered for word in ("waiting", "pausing", "random", "timeout", "stopped")
    ):
        return COLORS["warning"]
    if level == "step" or "started" in lowered:
        return COLORS["primary"]
    return COLORS["text_secondary"]


def _format(color: str, *, bold: bool = False) -> QTextCharFormat:
    char_format = QTextCharFormat()
    char_format.setForeground(QColor(color))
    if bold:
        char_format.setFontWeight(QFont.Weight.Bold)
    return char_format


def _insert_body(cursor, text: str, color: str) -> None:
    position = 0
    for match in _LINK_RE.finditer(text):
        cursor.insertText(text[position : match.start()], _format(color))
        cursor.insertText(match.group(0), _format(COLORS["cyan"], bold=True))
        position = match.end()
    cursor.insertText(text[position:], _format(color))


def append_semantic_log(
    view: QTextEdit,
    message: str,
    *,
    level: str = "info",
    generated_timestamp: str | None = None,
    follow: bool = True,
) -> None:
    """Append one semantically colored line while retaining clean plain text."""
    message = humanize_log_durations(message)
    scroll_bar = view.verticalScrollBar()
    previous_scroll = scroll_bar.value()
    cursor = QTextCursor(view.document())
    cursor.movePosition(QTextCursor.MoveOperation.End)

    timestamp_match = _TIMESTAMP_RE.match(message)
    if timestamp_match:
        timestamp = timestamp_match.group(1)
        message = message[timestamp_match.end() :]
    else:
        timestamp = f"[{generated_timestamp}]" if generated_timestamp else ""
    if timestamp:
        cursor.insertText(f"{timestamp} ", _format(COLORS["text_tertiary"]))

    command_match = _COMMAND_RE.match(message)
    if command_match:
        short_id, command_id, remainder = command_match.groups()
        cursor.insertText(f"{short_id} ", _format(COLORS["text_tertiary"]))
        cursor.insertText(
            command_label(command_id),
            _format(command_color(command_id), bold=True),
        )
        _insert_body(cursor, remainder, message_color(remainder, level))
    else:
        body_color = message_color(message, level)
        _insert_body(cursor, message, body_color)
    cursor.insertText("\n")
    if follow:
        view.setTextCursor(cursor)
        view.ensureCursorVisible()
    else:
        scroll_bar.setValue(min(previous_scroll, scroll_bar.maximum()))
