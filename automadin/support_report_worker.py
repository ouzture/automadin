"""Isolated child-process entry point for support-report collection."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from automadin.services.support_report import (
    MAX_REPORT_RECORD_BYTES,
    build_support_report_from_database_path,
    support_report_to_record,
)

REQUEST_VERSION = 1
REQUEST_FILE_NAME = "request.json"
RESULT_FILE_NAME = "result.json"
MAX_REQUEST_BYTES = 4096
MAX_RESULT_BYTES = MAX_REPORT_RECORD_BYTES
_REQUEST_KEYS = frozenset(
    {"request_version", "database_path", "data_directory"}
)


def _read_bounded_regular_file(path: Path, maximum_bytes: int) -> bytes:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_size > maximum_bytes
    ):
        raise ValueError("Worker protocol file was invalid.")
    with path.open("rb") as stream:
        payload = stream.read(maximum_bytes + 1)
    if len(payload) != metadata.st_size or len(payload) > maximum_bytes:
        raise ValueError("Worker protocol file changed while it was read.")
    return payload


def _reject_duplicate_request_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Support-report request contained duplicate fields.")
        result[key] = value
    return result


def _decode_request(payload: bytes) -> tuple[Path, Path]:
    try:
        record = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_request_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("Support-report request contained a non-finite number.")
            ),
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise ValueError("Support-report request was invalid.") from exc
    if (
        type(record) is not dict
        or not all(type(key) is str for key in record)
        or frozenset(record) != _REQUEST_KEYS
        or type(record["request_version"]) is not int
        or record["request_version"] != REQUEST_VERSION
    ):
        raise ValueError("Support-report request was invalid.")

    paths: list[Path] = []
    for key in ("database_path", "data_directory"):
        raw_path = record[key]
        if (
            type(raw_path) is not str
            or not raw_path
            or len(raw_path) > 32_767
            or not all(character.isprintable() for character in raw_path)
        ):
            raise ValueError("Support-report request path was invalid.")
        path = Path(raw_path)
        if not path.is_absolute():
            raise ValueError("Support-report request path was invalid.")
        paths.append(path)
    return paths[0], paths[1]


def _bounded_result(record: dict[str, object]) -> bytes:
    encoded = json.dumps(
        record,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    payload = encoded + b"\n"
    if len(payload) > MAX_RESULT_BYTES:
        raise ValueError("Support-report result exceeded the protocol limit.")
    return payload


def _write_result_exclusively(path: Path, record: dict[str, object]) -> None:
    payload = _bounded_result(record)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def run_worker(request_path: str | Path) -> None:
    """Validate one private request and exclusively publish its report record."""
    workspace = Path.cwd().resolve()
    request = Path(request_path).expanduser().resolve()
    if request.parent != workspace or request.name != REQUEST_FILE_NAME:
        raise ValueError("Support-report request escaped its workspace.")

    database_path, data_directory = _decode_request(
        _read_bounded_regular_file(request, MAX_REQUEST_BYTES)
    )
    report = build_support_report_from_database_path(
        database_path,
        data_directory,
    )
    _write_result_exclusively(
        workspace / RESULT_FILE_NAME,
        support_report_to_record(report),
    )


def main(argv: list[str] | None = None) -> int:
    """Run without emitting exception details or protocol data to stdio."""
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        return 1
    try:
        run_worker(arguments[0])
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
