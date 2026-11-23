"""Allowlist-based, deterministic support-report generation.

The report is deliberately allowlist based.  It never reads diagnostic log
contents or emits database rows, user paths, environment variables, device
identities, URLs, profile/account details, or exception-controlled messages.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from automadin.build_info import packaged_app_version
from automadin.diagnostics import (
    DEFAULT_BACKUP_COUNT,
    LOG_DIRECTORY_NAME,
    LOG_FILE_NAME,
    RUNTIME_MANAGER_LOG_FILE_NAME,
)
from automadin.runtime import PORTABLE_MARKER, application_dir, is_frozen

REPORT_FORMAT_VERSION = 1
REPORT_RECORD_VERSION = 1
MAX_REPORT_CHARACTERS = 32 * 1024
MAX_REPORT_RECORD_BYTES = 64 * 1024
MAX_FIELD_CHARACTERS = 120

LOG_DIRECTORY_AVAILABLE = "Available"
LOG_DIRECTORY_EMPTY = "Available; no known log files yet"
LOG_DIRECTORY_MISSING = "Not created yet"
LOG_DIRECTORY_UNSAFE = "Unavailable; path safety check failed"
LOG_DIRECTORY_UNKNOWN = "Could not check"

_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_VERSION_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){1,3}"
    r"(?:(?:a|b|rc|post|dev)(?:0|[1-9][0-9]*))?",
    re.ASCII | re.IGNORECASE,
)
_RELEASE_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+){0,3}", re.ASCII)
_APPLICATION_MODES = frozenset({"Source", "Portable", "Installed"})
_OPERATING_SYSTEMS = {
    "windows": "Windows",
    "linux": "Linux",
    "darwin": "Darwin",
    "freebsd": "FreeBSD",
    "openbsd": "OpenBSD",
    "netbsd": "NetBSD",
}
_ARCHITECTURES = {
    "amd64": "AMD64",
    "x86_64": "x86_64",
    "x86": "x86",
    "i386": "i386",
    "i686": "i686",
    "arm64": "ARM64",
    "aarch64": "aarch64",
}


class SupportReportDatabase(Protocol):
    """Read-only database surface used by support-report collection."""

    def get_workspace_statistics(self) -> dict[str, int]: ...


@dataclass(frozen=True, slots=True)
class _CollectedStatisticsDatabase:
    statistics: dict[str, int]

    def get_workspace_statistics(self) -> dict[str, int]:
        return self.statistics


@dataclass(frozen=True, slots=True)
class WorkspaceSummary:
    schema_version: int | None
    flows: int | None
    images: int | None
    run_setups: int | None
    browser_profiles: int | None


@dataclass(frozen=True, slots=True)
class DiagnosticFileSummary:
    name: str
    size_bytes: int
    modified_utc: datetime | None


@dataclass(frozen=True, slots=True)
class DiagnosticFileInventory:
    status: str
    directory: Path | None
    files: tuple[DiagnosticFileSummary, ...]


@dataclass(frozen=True, slots=True)
class SupportReportSnapshot:
    generated_utc: datetime
    application_version: str
    application_mode: str
    operating_system: str
    operating_system_release: str
    architecture: str
    python_version: str
    pyside_version: str
    workspace: WorkspaceSummary
    diagnostics: DiagnosticFileInventory


@dataclass(frozen=True, slots=True)
class SupportReport:
    """Exact preview/copy text plus an optional validated logs directory."""

    text: str
    logs_directory: Path | None
    snapshot: SupportReportSnapshot


def _validated_count(value: object) -> int | None:
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(value, bool):
        return None
    return count if 0 <= count <= 1_000_000_000 else None


def _validated_version(value: object, *, allow_development: bool = False) -> str:
    if not isinstance(value, str) or len(value) > MAX_FIELD_CHARACTERS:
        return "Could not check"
    candidate = value.strip()
    if allow_development and candidate == "development":
        return candidate
    if (
        _VERSION_PATTERN.fullmatch(candidate)
    ):
        return candidate
    return "Could not check"


def _validated_release(value: object) -> str:
    if not isinstance(value, str) or len(value) > MAX_FIELD_CHARACTERS:
        return "Could not check"
    candidate = value.strip()
    return (
        candidate
        if _RELEASE_PATTERN.fullmatch(candidate)
        else "Could not check"
    )


def _validated_choice(value: object, choices: Mapping[str, str]) -> str:
    if not isinstance(value, str) or len(value) > MAX_FIELD_CHARACTERS:
        return "Could not check"
    return choices.get(value.strip().casefold(), "Could not check")


def _validated_application_mode(value: object) -> str:
    if not isinstance(value, str) or len(value) > MAX_FIELD_CHARACTERS:
        return "Could not check"
    candidate = value.strip()
    return candidate if candidate in _APPLICATION_MODES else "Could not check"


def _validated_diagnostic_status(value: object) -> str:
    statuses = {
        LOG_DIRECTORY_AVAILABLE,
        LOG_DIRECTORY_EMPTY,
        LOG_DIRECTORY_MISSING,
        LOG_DIRECTORY_UNSAFE,
        LOG_DIRECTORY_UNKNOWN,
    }
    return (
        value
        if isinstance(value, str)
        and len(value) <= MAX_FIELD_CHARACTERS
        and value in statuses
        else LOG_DIRECTORY_UNKNOWN
    )


def _workspace_summary(database: SupportReportDatabase) -> WorkspaceSummary:
    try:
        statistics = database.get_workspace_statistics()
    except Exception:
        statistics = {}
    if not isinstance(statistics, Mapping):
        statistics = {}
    return WorkspaceSummary(
        schema_version=_validated_count(statistics.get("schema_version")),
        flows=_validated_count(statistics.get("flows")),
        images=_validated_count(statistics.get("images")),
        run_setups=_validated_count(statistics.get("run_setups")),
        browser_profiles=_validated_count(statistics.get("browser_profiles")),
    )


def _application_mode() -> str:
    try:
        if not is_frozen():
            return "Source"
        if (application_dir() / PORTABLE_MARKER).is_file():
            return "Portable"
        return "Installed"
    except Exception:
        return "Could not check"


def _installed_distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except (importlib.metadata.PackageNotFoundError, OSError, ValueError):
        return "Could not check"


def _is_reparse_point(information: os.stat_result) -> bool:
    return bool(
        stat.S_ISLNK(information.st_mode)
        or getattr(information, "st_file_attributes", 0)
        & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _known_log_names() -> tuple[str, ...]:
    names: list[str] = []
    for base_name in (LOG_FILE_NAME, RUNTIME_MANAGER_LOG_FILE_NAME):
        names.append(base_name)
        names.extend(
            f"{base_name}.{index}"
            for index in range(1, DEFAULT_BACKUP_COUNT + 1)
        )
    return tuple(names)


def inspect_diagnostic_files(data_dir: str | Path) -> DiagnosticFileInventory:
    """Inspect metadata for fixed Automadin logs without reading their contents."""
    try:
        canonical_data_dir = Path(data_dir).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return DiagnosticFileInventory(LOG_DIRECTORY_UNKNOWN, None, ())

    candidate = canonical_data_dir / LOG_DIRECTORY_NAME
    try:
        directory_information = candidate.lstat()
    except FileNotFoundError:
        return DiagnosticFileInventory(LOG_DIRECTORY_MISSING, None, ())
    except OSError:
        return DiagnosticFileInventory(LOG_DIRECTORY_UNKNOWN, None, ())

    if _is_reparse_point(directory_information) or not stat.S_ISDIR(
        directory_information.st_mode
    ):
        return DiagnosticFileInventory(LOG_DIRECTORY_UNSAFE, None, ())

    try:
        resolved_directory = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return DiagnosticFileInventory(LOG_DIRECTORY_UNKNOWN, None, ())
    if os.path.normcase(str(resolved_directory)) != os.path.normcase(str(candidate)):
        return DiagnosticFileInventory(LOG_DIRECTORY_UNSAFE, None, ())

    summaries: list[DiagnosticFileSummary] = []
    incomplete = False
    for name in _known_log_names():
        path = resolved_directory / name
        try:
            information = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            incomplete = True
            continue
        if _is_reparse_point(information) or not stat.S_ISREG(information.st_mode):
            incomplete = True
            continue
        try:
            modified_utc = datetime.fromtimestamp(
                information.st_mtime,
                timezone.utc,
            )
        except (OSError, OverflowError, ValueError):
            modified_utc = None
        summaries.append(
            DiagnosticFileSummary(
                name=name,
                size_bytes=max(0, int(information.st_size)),
                modified_utc=modified_utc,
            )
        )

    if incomplete:
        status = LOG_DIRECTORY_UNKNOWN
    elif summaries:
        status = LOG_DIRECTORY_AVAILABLE
    else:
        status = LOG_DIRECTORY_EMPTY
    return DiagnosticFileInventory(
        status=status,
        directory=resolved_directory,
        files=tuple(summaries),
    )


def validated_logs_directory(path: str | Path) -> Path | None:
    """Revalidate a previously reported logs directory immediately before opening."""
    candidate = Path(path)
    if candidate.name != LOG_DIRECTORY_NAME:
        return None
    inventory = inspect_diagnostic_files(candidate.parent)
    if inventory.directory is None:
        return None
    try:
        requested = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return (
        inventory.directory
        if os.path.normcase(str(inventory.directory))
        == os.path.normcase(str(requested))
        else None
    )


def _format_utc(value: datetime | None) -> str:
    if value is None:
        return "Could not check"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _format_count(value: object) -> str:
    validated = _validated_count(value)
    return str(validated) if validated is not None else "Could not check"


def _parse_utc(value: object, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > 40 or not value.endswith("Z"):
        raise ValueError("Support-report timestamp was invalid.")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("Support-report timestamp was invalid.") from exc
    if _format_utc(parsed) != value:
        raise ValueError("Support-report timestamp was not canonical.")
    return parsed


def _exact_mapping(
    value: object,
    expected_keys: frozenset[str],
    *,
    label: str,
) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != expected_keys:
        raise ValueError(f"Support-report {label} fields were invalid.")
    if not all(type(key) is str for key in value):
        raise ValueError(f"Support-report {label} keys were invalid.")
    return value


def _canonical_wire_value(
    value: object,
    validator: Callable[[object], str],
    *,
    label: str,
) -> str:
    if type(value) is not str or len(value) > MAX_FIELD_CHARACTERS:
        raise ValueError(f"Support-report {label} was invalid.")
    canonical = validator(value)
    if canonical != value:
        raise ValueError(f"Support-report {label} was not canonical.")
    return value


def _canonical_wire_count(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 1_000_000_000:
        raise ValueError(f"Support-report {label} was invalid.")
    return value


def _canonical_logs_directory(value: object) -> Path | None:
    if value is None:
        return None
    if type(value) is not str or not value or len(value) > 32_767:
        raise ValueError("Support-report logs directory was invalid.")
    if not all(character.isprintable() for character in value):
        raise ValueError("Support-report logs directory was invalid.")
    path = Path(value)
    if not path.is_absolute() or path.name != LOG_DIRECTORY_NAME or str(path) != value:
        raise ValueError("Support-report logs directory was not canonical.")
    return path


def _validate_diagnostic_invariants(
    status: str,
    files: tuple[DiagnosticFileSummary, ...],
    logs_directory: Path | None,
) -> None:
    if status == LOG_DIRECTORY_AVAILABLE and (not files or logs_directory is None):
        raise ValueError("Support-report diagnostic state was inconsistent.")
    if status == LOG_DIRECTORY_EMPTY and (files or logs_directory is None):
        raise ValueError("Support-report diagnostic state was inconsistent.")
    if status in {LOG_DIRECTORY_MISSING, LOG_DIRECTORY_UNSAFE} and (
        files or logs_directory is not None
    ):
        raise ValueError("Support-report diagnostic state was inconsistent.")
    if files and logs_directory is None:
        raise ValueError("Support-report diagnostic state was inconsistent.")


def _canonical_diagnostic_files(
    values: object,
    *,
    require_canonical_order: bool,
) -> tuple[DiagnosticFileSummary, ...]:
    if type(values) is not list or len(values) > len(_known_log_names()):
        raise ValueError("Support-report diagnostic-file list was invalid.")
    known_names = frozenset(_known_log_names())
    files: list[DiagnosticFileSummary] = []
    seen_names: set[str] = set()
    for raw_file in values:
        item = _exact_mapping(
            raw_file,
            frozenset({"name", "size_bytes", "modified_utc"}),
            label="diagnostic file",
        )
        name = item["name"]
        if type(name) is not str or name not in known_names or name in seen_names:
            raise ValueError("Support-report diagnostic-file metadata was invalid.")
        seen_names.add(name)
        size_bytes = _canonical_wire_count(
            item["size_bytes"],
            label="diagnostic-file size",
        )
        assert size_bytes is not None
        files.append(
            DiagnosticFileSummary(
                name=name,
                size_bytes=size_bytes,
                modified_utc=_parse_utc(item["modified_utc"], optional=True),
            )
        )
    ordered = sorted(files, key=lambda item: item.name)
    if require_canonical_order and files != ordered:
        raise ValueError("Support-report diagnostic-file order was not canonical.")
    return tuple(ordered)


def support_report_to_record(report: SupportReport) -> dict[str, object]:
    """Serialize only structured report facts for the helper-process protocol."""
    snapshot = report.snapshot
    application_version = _validated_version(
        snapshot.application_version,
        allow_development=True,
    )
    application_mode = _validated_application_mode(snapshot.application_mode)
    operating_system = _validated_choice(
        snapshot.operating_system,
        _OPERATING_SYSTEMS,
    )
    release = _validated_release(snapshot.operating_system_release)
    architecture = _validated_choice(snapshot.architecture, _ARCHITECTURES)
    python_version = _validated_version(snapshot.python_version)
    pyside_version = _validated_version(snapshot.pyside_version)
    status = _validated_diagnostic_status(snapshot.diagnostics.status)

    diagnostic_files = snapshot.diagnostics.files
    if (
        type(diagnostic_files) is not tuple
        or len(diagnostic_files) > len(_known_log_names())
    ):
        raise ValueError("Support-report diagnostic-file list was invalid.")
    raw_files: list[dict[str, object]] = []
    for item in diagnostic_files:
        if type(item) is not DiagnosticFileSummary:
            raise ValueError("Support-report diagnostic-file metadata was invalid.")
        raw_files.append(
            {
                "name": item.name,
                "size_bytes": item.size_bytes,
                "modified_utc": (
                    _format_utc(item.modified_utc)
                    if item.modified_utc is not None
                    else None
                ),
            }
        )
    files = _canonical_diagnostic_files(
        raw_files,
        require_canonical_order=False,
    )

    if report.logs_directory != snapshot.diagnostics.directory:
        raise ValueError("Support-report logs-directory state was inconsistent.")
    logs_directory = _canonical_logs_directory(
        str(report.logs_directory) if report.logs_directory is not None else None
    )
    _validate_diagnostic_invariants(status, files, logs_directory)

    record: dict[str, object] = {
        "record_version": REPORT_RECORD_VERSION,
        "generated_utc": _format_utc(snapshot.generated_utc),
        "application": {
            "version": application_version,
            "mode": application_mode,
        },
        "platform": {
            "operating_system": operating_system,
            "release": release,
            "architecture": architecture,
            "python": python_version,
            "pyside6": pyside_version,
        },
        "workspace": {
            "schema_version": _canonical_wire_count(
                snapshot.workspace.schema_version,
                label="workspace count",
            ),
            "flows": _canonical_wire_count(
                snapshot.workspace.flows,
                label="workspace count",
            ),
            "images": _canonical_wire_count(
                snapshot.workspace.images,
                label="workspace count",
            ),
            "run_setups": _canonical_wire_count(
                snapshot.workspace.run_setups,
                label="workspace count",
            ),
            "browser_profiles": _canonical_wire_count(
                snapshot.workspace.browser_profiles,
                label="workspace count",
            ),
        },
        "diagnostics": {
            "status": status,
            "files": [
                {
                    "name": item.name,
                    "size_bytes": item.size_bytes,
                    "modified_utc": (
                        _format_utc(item.modified_utc)
                        if item.modified_utc is not None
                        else None
                    ),
                }
                for item in files
            ],
        },
        "logs_directory": str(logs_directory) if logs_directory is not None else None,
    }
    encoded = json.dumps(
        record,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_REPORT_RECORD_BYTES:
        raise ValueError("Support-report record exceeded the protocol limit.")
    return record


def support_report_from_record(record: object) -> SupportReport:
    """Validate and reconstruct a report returned by the isolated helper."""
    root = _exact_mapping(
        record,
        frozenset(
            {
                "record_version",
                "generated_utc",
                "application",
                "platform",
                "workspace",
                "diagnostics",
                "logs_directory",
            }
        ),
        label="record",
    )
    if (
        type(root["record_version"]) is not int
        or root["record_version"] != REPORT_RECORD_VERSION
    ):
        raise ValueError("Support-report record version was unsupported.")
    generated_utc = _parse_utc(root["generated_utc"])
    assert generated_utc is not None

    application = _exact_mapping(
        root["application"],
        frozenset({"version", "mode"}),
        label="application",
    )
    platform_record = _exact_mapping(
        root["platform"],
        frozenset(
            {
                "operating_system",
                "release",
                "architecture",
                "python",
                "pyside6",
            }
        ),
        label="platform",
    )
    workspace_record = _exact_mapping(
        root["workspace"],
        frozenset(
            {
                "schema_version",
                "flows",
                "images",
                "run_setups",
                "browser_profiles",
            }
        ),
        label="workspace",
    )

    diagnostics_record = _exact_mapping(
        root["diagnostics"],
        frozenset({"status", "files"}),
        label="diagnostics",
    )
    files = _canonical_diagnostic_files(
        diagnostics_record["files"],
        require_canonical_order=True,
    )

    raw_status = diagnostics_record["status"]
    status = _canonical_wire_value(
        raw_status,
        _validated_diagnostic_status,
        label="diagnostic status",
    )
    logs_directory = _canonical_logs_directory(root["logs_directory"])
    _validate_diagnostic_invariants(status, files, logs_directory)

    snapshot = SupportReportSnapshot(
        generated_utc=generated_utc,
        application_version=_canonical_wire_value(
            application["version"],
            lambda value: _validated_version(value, allow_development=True),
            label="application version",
        ),
        application_mode=_canonical_wire_value(
            application["mode"],
            _validated_application_mode,
            label="application mode",
        ),
        operating_system=_canonical_wire_value(
            platform_record["operating_system"],
            lambda value: _validated_choice(value, _OPERATING_SYSTEMS),
            label="operating system",
        ),
        operating_system_release=_canonical_wire_value(
            platform_record["release"],
            _validated_release,
            label="operating-system release",
        ),
        architecture=_canonical_wire_value(
            platform_record["architecture"],
            lambda value: _validated_choice(value, _ARCHITECTURES),
            label="architecture",
        ),
        python_version=_canonical_wire_value(
            platform_record["python"],
            _validated_version,
            label="Python version",
        ),
        pyside_version=_canonical_wire_value(
            platform_record["pyside6"],
            _validated_version,
            label="PySide6 version",
        ),
        workspace=WorkspaceSummary(
            schema_version=_canonical_wire_count(
                workspace_record["schema_version"],
                label="workspace count",
            ),
            flows=_canonical_wire_count(
                workspace_record["flows"],
                label="workspace count",
            ),
            images=_canonical_wire_count(
                workspace_record["images"],
                label="workspace count",
            ),
            run_setups=_canonical_wire_count(
                workspace_record["run_setups"],
                label="workspace count",
            ),
            browser_profiles=_canonical_wire_count(
                workspace_record["browser_profiles"],
                label="workspace count",
            ),
        ),
        diagnostics=DiagnosticFileInventory(
            status=status,
            directory=logs_directory,
            files=files,
        ),
    )
    return SupportReport(
        text=format_support_report(snapshot),
        logs_directory=logs_directory,
        snapshot=snapshot,
    )


def _reject_duplicate_record_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Support-report record contained duplicate fields.")
        result[key] = value
    return result


def decode_support_report_record_json(payload: bytes) -> SupportReport:
    """Decode one bounded UTF-8 JSON helper result using the strict wire schema."""
    if (
        type(payload) is not bytes
        or not payload
        or len(payload) > MAX_REPORT_RECORD_BYTES
    ):
        raise ValueError("Support-report record payload was invalid.")
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_record_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("Support-report record contained a non-finite number.")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError("Support-report record JSON was invalid.") from exc
    return support_report_from_record(decoded)


def format_support_report(snapshot: SupportReportSnapshot) -> str:
    """Format one bounded report whose exact copied text is previewed first."""
    workspace = snapshot.workspace
    lines = [
        "AUTOMADIN SUPPORT REPORT",
        f"Report format: {REPORT_FORMAT_VERSION}",
        f"Generated (UTC): {_format_utc(snapshot.generated_utc)}",
        "",
        "PRIVACY",
        (
            "Includes app, platform, and runtime versions; aggregate workspace "
            "counts; and fixed Automadin log filenames, sizes, and timestamps."
        ),
        (
            "The copied report does not include database records, workflow/profile/"
            "device/account names, URLs, environment variables, local paths, or "
            "log messages."
        ),
        "",
        "APPLICATION",
        (
            "Version: "
            f"{_validated_version(snapshot.application_version, allow_development=True)}"
        ),
        f"Mode: {_validated_application_mode(snapshot.application_mode)}",
        "",
        "PLATFORM",
        (
            "Operating system: "
            f"{_validated_choice(snapshot.operating_system, _OPERATING_SYSTEMS)}"
        ),
        f"OS release: {_validated_release(snapshot.operating_system_release)}",
        f"Architecture: {_validated_choice(snapshot.architecture, _ARCHITECTURES)}",
        f"Python: {_validated_version(snapshot.python_version)}",
        f"PySide6: {_validated_version(snapshot.pyside_version)}",
        "",
        "WORKSPACE",
        f"Database schema: {_format_count(workspace.schema_version)}",
        f"Flows: {_format_count(workspace.flows)}",
        f"Images: {_format_count(workspace.images)}",
        f"Run setups: {_format_count(workspace.run_setups)}",
        f"Browser profiles: {_format_count(workspace.browser_profiles)}",
        "",
        "DIAGNOSTIC FILES",
        f"Directory: {_validated_diagnostic_status(snapshot.diagnostics.status)}",
    ]
    if snapshot.diagnostics.files:
        known_log_names = frozenset(_known_log_names())
        for diagnostic_file in snapshot.diagnostics.files:
            if diagnostic_file.name not in known_log_names:
                continue
            size_bytes = _validated_count(diagnostic_file.size_bytes)
            if size_bytes is None:
                continue
            lines.append(
                f"- {diagnostic_file.name} | {size_bytes:,} bytes | "
                f"modified {_format_utc(diagnostic_file.modified_utc)}"
            )
    if lines[-1].startswith("Directory:"):
        lines.append("- No known diagnostic file metadata is available.")
    text = "\n".join(lines) + "\n"
    if len(text) > MAX_REPORT_CHARACTERS:
        raise ValueError("Support report exceeded its fixed size limit.")
    return text


def build_support_report(
    database: SupportReportDatabase,
    data_dir: str | Path,
    *,
    generated_utc: datetime | None = None,
) -> SupportReport:
    """Collect allowlisted local facts and return the exact preview/copy report."""
    generated = generated_utc or datetime.now(timezone.utc)
    diagnostics = inspect_diagnostic_files(data_dir)
    snapshot = SupportReportSnapshot(
        generated_utc=generated,
        application_version=packaged_app_version("development"),
        application_mode=_application_mode(),
        operating_system=platform.system() or "Could not check",
        operating_system_release=platform.release() or "Could not check",
        architecture=platform.machine() or "Could not check",
        python_version=platform.python_version() or "Could not check",
        pyside_version=_installed_distribution_version("PySide6"),
        workspace=_workspace_summary(database),
        diagnostics=diagnostics,
    )
    return SupportReport(
        text=format_support_report(snapshot),
        logs_directory=diagnostics.directory,
        snapshot=snapshot,
    )


def build_support_report_from_database_path(
    db_path: str | Path,
    data_dir: str | Path,
    *,
    database_timeout_seconds: float = 1.0,
    generated_utc: datetime | None = None,
) -> SupportReport:
    """Build a report using a dedicated, bounded, read-only SQLite connection."""
    from automadin.services.database import read_workspace_statistics

    try:
        statistics = read_workspace_statistics(
            db_path,
            timeout_seconds=database_timeout_seconds,
        )
    except Exception:
        statistics = {}
    return build_support_report(
        _CollectedStatisticsDatabase(statistics),
        data_dir,
        generated_utc=generated_utc,
    )
