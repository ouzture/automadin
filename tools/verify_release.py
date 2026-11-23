"""Fail when a Windows release is incomplete or contains user data."""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path

try:
    from tools.installer_manifest import verify_installer_manifest
    from tools.verify_release_compliance import verify_release_compliance
except ModuleNotFoundError as exc:
    if exc.name != "tools":
        raise
    from installer_manifest import verify_installer_manifest
    from verify_release_compliance import verify_release_compliance


SQLITE_HEADER = b"SQLite format 3\x00"
SQLITE_WAL_MAGICS = {b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83"}
SQLITE_ROLLBACK_JOURNAL_HEADER = b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"
SQLITE_NAME_SUFFIXES = (
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".sqlite",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-journal",
    ".sqlite3-shm",
    ".sqlite3-wal",
)
AUTOMADIN_DATA_FILE_PATTERN = re.compile(
    r"^\.?automadin(?:"
    r"\.db.*|"
    r"[-_.](?:backup|before-import|import-pending|import-rejected|"
    r"portability-pending|portability-rejected|portability-orphaned|"
    r"portability-staging)(?:[-_.].*)?"
    r")$",
    re.IGNORECASE,
)
PORTABLE_BACKUP_SUFFIX = ".automadin-bundle"
USER_DATA_DIRECTORY_PATTERN = re.compile(
    r"^(?:"
    r"\.?(?:profiles|data)(?:[-_.](?:backup|pending|rejected)(?:[-_.].*)?)?|"
    r"\.(?:creating|importing|deleting)-"
    r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}-[0-9a-f]{32}"
    r")$",
    re.IGNORECASE,
)
MINIMUM_PE_FILE_SIZE = 512
PE_SIGNATURE = b"PE\x00\x00"
PE_EXECUTABLE_IMAGE = 0x0002
PE_OPTIONAL_HEADER_MAGICS = {0x010B, 0x020B}
PE_WINDOWS_GUI_SUBSYSTEM = 2
PE_MACHINES = {
    0x014C,  # Intel 386
    0x8664,  # AMD64
    0xAA64,  # ARM64
}
MINIMUM_PLAYWRIGHT_CLI_SIZE = 32
RELEASE_MANIFEST_PACKAGES = {"Nuitka", "PySide6", "playwright", "pywin32"}
JAVASCRIPT_MODULE_PATTERN = re.compile(
    r"(?:\brequire\s*\(|(?:^|\n)\s*(?:import|export)\b)",
    re.MULTILINE,
)


def _is_user_data_file_name(name: str) -> bool:
    lower_name = name.lower()
    return (
        lower_name.endswith(SQLITE_NAME_SUFFIXES)
        or lower_name.endswith(PORTABLE_BACKUP_SUFFIX)
        or bool(AUTOMADIN_DATA_FILE_PATTERN.fullmatch(name))
    )


def _sqlite_content_kind(path: Path) -> str | None:
    with path.open("rb") as release_file:
        header = release_file.read(len(SQLITE_HEADER))
    if header == SQLITE_HEADER:
        return "database"
    if header[:4] in SQLITE_WAL_MAGICS:
        return "write-ahead log"
    if header[: len(SQLITE_ROLLBACK_JOURNAL_HEADER)] == SQLITE_ROLLBACK_JOURNAL_HEADER:
        return "rollback journal"
    return None


def _windows_executable_problem(
    path: Path, *, expected_subsystem: int | None = None
) -> str | None:
    """Return why *path* is not a structurally plausible Windows executable."""
    try:
        file_size = path.stat().st_size
        if file_size < MINIMUM_PE_FILE_SIZE:
            return f"file is too small ({file_size} bytes)"

        with path.open("rb") as executable:
            dos_header = executable.read(64)
            if len(dos_header) != 64 or dos_header[:2] != b"MZ":
                return "DOS MZ header is missing"

            pe_offset = struct.unpack_from("<I", dos_header, 0x3C)[0]
            if pe_offset < 64 or pe_offset + 24 > file_size:
                return "PE header offset is outside the file"

            executable.seek(pe_offset)
            pe_header = executable.read(24)
            if len(pe_header) != 24 or pe_header[:4] != PE_SIGNATURE:
                return "PE signature is missing"

            machine, section_count = struct.unpack_from("<HH", pe_header, 4)
            optional_header_size, characteristics = struct.unpack_from(
                "<HH", pe_header, 20
            )
            if machine not in PE_MACHINES:
                return f"unsupported PE machine type 0x{machine:04x}"
            if not 1 <= section_count <= 96:
                return f"invalid PE section count {section_count}"
            if not characteristics & PE_EXECUTABLE_IMAGE:
                return "PE header is not marked as an executable image"
            if optional_header_size < 2:
                return "PE optional header is missing"

            section_table_end = (
                pe_offset + 24 + optional_header_size + (section_count * 40)
            )
            if section_table_end > file_size:
                return "PE headers are truncated"

            optional_magic_bytes = executable.read(2)
            if len(optional_magic_bytes) != 2:
                return "PE optional header is truncated"
            optional_magic = struct.unpack("<H", optional_magic_bytes)[0]
            if optional_magic not in PE_OPTIONAL_HEADER_MAGICS:
                return f"invalid PE optional-header magic 0x{optional_magic:04x}"
            minimum_optional_size = 96 if optional_magic == 0x010B else 112
            if optional_header_size < minimum_optional_size:
                return "PE optional header is truncated"
            if expected_subsystem is not None:
                executable.seek(pe_offset + 24 + 68)
                subsystem_bytes = executable.read(2)
                if len(subsystem_bytes) != 2:
                    return "PE optional header is truncated before subsystem"
                subsystem = struct.unpack("<H", subsystem_bytes)[0]
                if subsystem != expected_subsystem:
                    return (
                        f"PE subsystem is {subsystem}, expected "
                        f"{expected_subsystem} (Windows GUI)"
                    )
    except OSError as exc:
        return f"could not inspect file: {exc}"
    return None


def _playwright_cli_problem(path: Path) -> str | None:
    """Return why *path* is not a plausible Playwright Node CLI entrypoint."""
    try:
        file_size = path.stat().st_size
        if file_size < MINIMUM_PLAYWRIGHT_CLI_SIZE:
            return f"file is too small ({file_size} bytes)"
        with path.open("rb") as cli_file:
            content = cli_file.read(64 * 1024)
    except OSError as exc:
        return f"could not inspect file: {exc}"

    if b"\x00" in content:
        return "file contains binary NUL bytes"
    try:
        source = content.decode("utf-8")
    except UnicodeDecodeError:
        return "file is not valid UTF-8 JavaScript"
    if not JAVASCRIPT_MODULE_PATTERN.search(source):
        return "file does not load or declare a JavaScript module"
    return None


def verify_release(
    release_dir: Path,
    *,
    require_compliance_ready: bool = False,
    project_root: Path | None = None,
    lock_path: Path | None = None,
    policy_path: Path | None = None,
    source_dir: Path | None = None,
    source_policy_path: Path | None = None,
    native_policy_path: Path | None = None,
    inno_compiler: Path | None = None,
) -> list[str]:
    errors: list[str] = []
    if not release_dir.is_dir():
        return [f"Release directory does not exist: {release_dir}"]

    errors.extend(verify_installer_manifest(release_dir))

    automadin_executable = release_dir / "Automadin.exe"
    if not automadin_executable.is_file():
        errors.append("Automadin.exe is missing")
    else:
        executable_problem = _windows_executable_problem(
            automadin_executable, expected_subsystem=PE_WINDOWS_GUI_SUBSYSTEM
        )
        if executable_problem:
            errors.append(f"Automadin.exe is invalid: {executable_problem}")
    if (release_dir / "main.exe").exists():
        errors.append("Stale generated main.exe was not normalized")

    qt_network = release_dir / "qt6network.dll"
    if not qt_network.is_file():
        errors.append(
            "Qt Network runtime is missing; existing-window activation cannot work"
        )
    else:
        network_problem = _windows_executable_problem(qt_network)
        if network_problem:
            errors.append(f"qt6network.dll is invalid: {network_problem}")

    qwindows_candidates = (
        release_dir / "PySide6" / "qt-plugins" / "platforms" / "qwindows.dll",
        release_dir / "PySide6" / "plugins" / "platforms" / "qwindows.dll",
        release_dir / "platforms" / "qwindows.dll",
    )
    qwindows = next(
        (candidate for candidate in qwindows_candidates if candidate.is_file()),
        None,
    )
    if qwindows is None:
        errors.append(
            "Qt Windows platform plugin is missing; expected qwindows.dll under "
            "PySide6/qt-plugins/platforms, PySide6/plugins/platforms, or platforms"
        )
    else:
        qwindows_problem = _windows_executable_problem(qwindows)
        if qwindows_problem:
            errors.append(f"qwindows.dll is invalid: {qwindows_problem}")

    manifest: dict[str, object] | None = None
    manifest_path = release_dir / "release-manifest.json"
    if not manifest_path.is_file():
        errors.append("release-manifest.json is missing")
    else:
        try:
            manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest_value, dict):
                raise AttributeError("manifest root must be an object")
            manifest = manifest_value
            if manifest.get("schema_version") != 1:
                errors.append("release-manifest.json has an unsupported schema version")
            if manifest.get("application", {}).get("name") != "Automadin":
                errors.append("release-manifest.json has an invalid application name")
            version = manifest.get("application", {}).get("version")
            if not isinstance(version, str) or not version.strip():
                errors.append("release-manifest.json has no application version")
            python_version = manifest.get("build", {}).get("python_version")
            if not isinstance(python_version, str) or not python_version.strip():
                errors.append("release-manifest.json has no Python build version")
            packages = manifest.get("build", {}).get("packages")
            if not isinstance(packages, dict):
                errors.append("release-manifest.json has no package version mapping")
            else:
                for package in sorted(RELEASE_MANIFEST_PACKAGES):
                    package_version = packages.get(package)
                    if not isinstance(package_version, str) or not package_version.strip():
                        errors.append(
                            "release-manifest.json has no build version for "
                            f"{package}"
                        )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
            errors.append(f"release-manifest.json is invalid: {exc}")
    if manifest is not None:
        errors.extend(
            verify_release_compliance(
                release_dir,
                manifest,
                require_compliance_ready=require_compliance_ready,
                project_root=project_root,
                lock_path=lock_path,
                policy_path=policy_path,
                source_dir=source_dir,
                source_policy_path=source_policy_path,
                native_policy_path=native_policy_path,
                inno_compiler=inno_compiler,
            )
        )

    if (release_dir / "browsers").exists():
        errors.append("Release must not bundle Playwright browser binaries")
    if (release_dir / "tools" / "platform-tools").exists():
        errors.append("Release must not redistribute Android Platform Tools")

    driver = release_dir / "playwright" / "driver"
    node_executable = driver / "node.exe"
    if not node_executable.is_file():
        errors.append("Playwright installer driver is missing: node.exe")
    else:
        executable_problem = _windows_executable_problem(node_executable)
        if executable_problem:
            errors.append(
                "Playwright installer driver node.exe is invalid: "
                f"{executable_problem}"
            )

    playwright_cli = driver / "package" / "cli.js"
    if not playwright_cli.is_file():
        errors.append("Playwright installer driver is missing: cli.js")
    else:
        cli_problem = _playwright_cli_problem(playwright_cli)
        if cli_problem:
            errors.append(
                f"Playwright installer driver cli.js is invalid: {cli_problem}"
            )

    for path in release_dir.rglob("*"):
        relative_path = path.relative_to(release_dir)
        if path.is_dir() and USER_DATA_DIRECTORY_PATTERN.fullmatch(path.name):
            errors.append(f"Release contains a user-data directory: {relative_path}")
            continue
        if not path.is_file():
            continue
        if _is_user_data_file_name(path.name):
            errors.append(
                f"Release contains a SQLite database/user-data file by name: {relative_path}"
            )
            continue
        try:
            sqlite_kind = _sqlite_content_kind(path)
        except OSError as exc:
            errors.append(f"Could not inspect release file {relative_path}: {exc}")
            continue
        if sqlite_kind:
            errors.append(f"Release contains SQLite {sqlite_kind} content: {relative_path}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--require-compliance-ready", action="store_true")
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--lock", dest="lock_path", type=Path)
    parser.add_argument("--policy", dest="policy_path", type=Path)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--source-policy", dest="source_policy_path", type=Path)
    parser.add_argument("--native-policy", dest="native_policy_path", type=Path)
    parser.add_argument("--inno-compiler", type=Path)
    args = parser.parse_args()

    errors = verify_release(
        args.release_dir.resolve(),
        require_compliance_ready=args.require_compliance_ready,
        project_root=args.project_root,
        lock_path=args.lock_path,
        policy_path=args.policy_path,
        source_dir=args.source_dir,
        source_policy_path=args.source_policy_path,
        native_policy_path=args.native_policy_path,
        inno_compiler=args.inno_compiler,
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Release verified: {args.release_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
