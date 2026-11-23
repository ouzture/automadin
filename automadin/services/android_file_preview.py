from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from xml.dom import minidom

from automadin.models.android_file import AndroidFilePreview


SQLITE_EXTENSIONS = {".db", ".sqlite", ".sqlite3"}
SQLITE_OBJECT_LIMIT = 200


def preview_text(device_path: str, text: str, truncated: bool) -> AndroidFilePreview:
    suffix = Path(device_path).suffix.lower()
    stripped = text.lstrip()

    if suffix == ".json" or stripped.startswith(("{", "[")):
        try:
            formatted = json.dumps(json.loads(text), indent=2, sort_keys=True)
            return AndroidFilePreview(
                path=device_path,
                kind="json",
                title="JSON",
                text=_with_truncation_note(formatted, truncated),
                truncated=truncated,
            )
        except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
            pass

    if suffix == ".xml" or stripped.startswith("<"):
        return AndroidFilePreview(
            path=device_path,
            kind="xml",
            title="XML",
            text=_with_truncation_note(_format_xml(text), truncated),
            truncated=truncated,
        )

    return AndroidFilePreview(
        path=device_path,
        kind="text",
        title="Text",
        text=_with_truncation_note(text, truncated),
        truncated=truncated,
    )


def preview_local_file(device_path: str, local_path: str, max_bytes: int = 262144) -> AndroidFilePreview:
    suffix = Path(device_path).suffix.lower()

    if suffix in SQLITE_EXTENSIONS:
        return _preview_sqlite(device_path, local_path)

    path = Path(local_path)
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    truncated = len(data) > max_bytes

    if _is_binary(data):
        return _binary_preview(device_path, truncated=truncated)

    if truncated:
        data = data[:max_bytes]

    text = data.decode("utf-8", errors="replace")
    return preview_text(device_path, text, truncated=truncated)


def _preview_sqlite(device_path: str, local_path: str) -> AndroidFilePreview:
    try:
        uri = f"{Path(local_path).resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            rows = connection.execute(
                """
                SELECT type, name
                FROM sqlite_master
                WHERE type IN ('table', 'view')
                  AND name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                LIMIT ?
                """,
                (SQLITE_OBJECT_LIMIT + 1,),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return _binary_preview(
            device_path,
            text=f"Binary file\n\nSQLite preview failed: {exc}",
            truncated=False,
        )

    if rows:
        truncated = len(rows) > SQLITE_OBJECT_LIMIT
        rows = rows[:SQLITE_OBJECT_LIMIT]
        lines = ["SQLite tables/views:"]
        lines.extend(f"- {name} ({kind})" for kind, name in rows)
        if truncated:
            lines.append(f"[Preview truncated after {SQLITE_OBJECT_LIMIT} objects]")
        text = "\n".join(lines)
    else:
        truncated = False
        text = "SQLite database\n\nNo tables or views found."

    return AndroidFilePreview(
        path=device_path,
        kind="sqlite",
        title="SQLite",
        text=text,
        truncated=truncated,
    )


def _format_xml(text: str) -> str:
    upper_text = text.upper()
    if "<!DOCTYPE" in upper_text or "<!ENTITY" in upper_text:
        return text

    try:
        parsed = minidom.parseString(text.encode("utf-8"))
        lines = parsed.toprettyxml(indent="  ").splitlines()
        return "\n".join(line for line in lines if line.strip())
    except Exception:
        return text


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data


def _binary_preview(device_path: str, text: str | None = None, truncated: bool = False) -> AndroidFilePreview:
    return AndroidFilePreview(
        path=device_path,
        kind="binary",
        title="Binary",
        text=_with_truncation_note(text or "Binary file", truncated),
        truncated=truncated,
    )


def _with_truncation_note(text: str, truncated: bool) -> str:
    if not truncated:
        return text
    return f"{text}\n\n[Preview truncated]"
