"""Create and validate the installer-owned file manifest for a release tree."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path, PurePosixPath


MANIFEST_NAME = "install-files.manifest"
MANIFEST_HEADER = "AUTOMADIN-INSTALL-MANIFEST-V1"
MAX_MANAGED_FILE_COUNT = 200_000
MAX_MANAGED_PATH_LENGTH = 1024
_WINDOWS_INVALID_CHARACTERS = frozenset(':*?<>|"')


class InstallerManifestError(ValueError):
    """The managed-file manifest is missing, malformed, or out of date."""


def _normalize_relative_path(value: str) -> str:
    raw_parts = value.split("/")
    path = PurePosixPath(value)
    if (
        not value
        or value != value.strip()
        or len(value) > MAX_MANAGED_PATH_LENGTH
        or "\\" in value
        or "\0" in value
        or any(ord(character) < 32 or ord(character) > 126 for character in value)
        or any(character in _WINDOWS_INVALID_CHARACTERS for character in value)
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in raw_parts)
        or any(part.endswith((".", " ")) for part in raw_parts)
    ):
        raise InstallerManifestError(f"unsafe managed-file path: {value!r}")
    normalized = path.as_posix()
    if _is_protected_user_path(normalized):
        raise InstallerManifestError(f"protected user-data path: {value!r}")
    return normalized


def _is_protected_user_path(value: str) -> bool:
    lower = value.lower()
    parts = lower.split("/")
    database_suffixes = (
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
    return (
        any(part in {"data", "profiles", "runtime", "logs"} for part in parts)
        or lower == "portable.flag"
        or lower.endswith(database_suffixes)
        or (
            lower.startswith("unins")
            and lower.endswith((".exe", ".dat", ".msg"))
        )
    )


def release_file_entries(release_dir: Path) -> tuple[str, ...]:
    """Return every regular release file, including the manifest itself."""
    root = release_dir.resolve(strict=True)
    if not root.is_dir():
        raise InstallerManifestError(f"release directory is not a directory: {root}")

    entries = {MANIFEST_NAME}
    casefolded: dict[str, str] = {MANIFEST_NAME.casefold(): MANIFEST_NAME}
    for candidate in root.rglob("*"):
        if candidate.is_symlink():
            raise InstallerManifestError(
                f"release tree contains a symbolic link: {candidate.relative_to(root)}"
            )
        if not candidate.is_file():
            continue
        relative = _normalize_relative_path(candidate.relative_to(root).as_posix())
        prior = casefolded.setdefault(relative.casefold(), relative)
        if prior != relative:
            raise InstallerManifestError(
                f"release tree has a Windows path collision: {prior!r} and {relative!r}"
            )
        entries.add(relative)
    if len(entries) > MAX_MANAGED_FILE_COUNT:
        raise InstallerManifestError(
            f"release tree has more than {MAX_MANAGED_FILE_COUNT} managed files"
        )
    return tuple(sorted(entries, key=lambda value: (value.lower(), value)))


def write_installer_manifest(release_dir: Path) -> Path:
    """Atomically write the exact release payload list consumed by the installer."""
    root = release_dir.resolve(strict=True)
    entries = release_file_entries(root)
    destination = root / MANIFEST_NAME
    content = "\r\n".join((MANIFEST_HEADER, *entries, ""))

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{MANIFEST_NAME}.",
        suffix=".tmp",
        dir=root,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def read_installer_manifest(path: Path) -> tuple[str, ...]:
    """Read a canonical installer manifest and reject unsafe or duplicate paths."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallerManifestError(f"could not read {path.name}: {exc}") from exc
    if not lines or lines[0] != MANIFEST_HEADER:
        raise InstallerManifestError(
            f"{path.name} has a missing or unsupported manifest header"
        )

    entries: list[str] = []
    seen: set[str] = set()
    for line in lines[1:]:
        if not line:
            raise InstallerManifestError(f"{path.name} contains a blank path")
        normalized = _normalize_relative_path(line)
        folded = normalized.casefold()
        if folded in seen:
            raise InstallerManifestError(
                f"{path.name} contains a duplicate Windows path: {normalized}"
            )
        seen.add(folded)
        entries.append(normalized)
    if len(entries) > MAX_MANAGED_FILE_COUNT:
        raise InstallerManifestError(
            f"{path.name} has more than {MAX_MANAGED_FILE_COUNT} managed files"
        )
    if MANIFEST_NAME.casefold() not in seen:
        raise InstallerManifestError(f"{path.name} does not list itself")
    if entries != sorted(entries, key=lambda value: (value.lower(), value)):
        raise InstallerManifestError(f"{path.name} paths are not canonically sorted")
    return tuple(entries)


def verify_installer_manifest(release_dir: Path) -> list[str]:
    """Return deterministic errors for a missing or stale managed-file manifest."""
    root = release_dir.resolve()
    manifest = root / MANIFEST_NAME
    if not manifest.is_file():
        return [f"{MANIFEST_NAME} is missing"]
    try:
        actual = read_installer_manifest(manifest)
        expected = release_file_entries(root)
    except (OSError, InstallerManifestError) as exc:
        return [f"{MANIFEST_NAME} is invalid: {exc}"]
    if actual == expected:
        return []

    actual_folded = {entry.casefold(): entry for entry in actual}
    expected_folded = {entry.casefold(): entry for entry in expected}
    missing = [
        expected_folded[key] for key in sorted(expected_folded.keys() - actual_folded.keys())
    ]
    stale = [
        actual_folded[key] for key in sorted(actual_folded.keys() - expected_folded.keys())
    ]
    details: list[str] = []
    if missing:
        details.append(f"unlisted release files: {', '.join(missing)}")
    if stale:
        details.append(f"paths absent from release: {', '.join(stale)}")
    return [f"{MANIFEST_NAME} is stale ({'; '.join(details)})"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = write_installer_manifest(args.release_dir)
    except (OSError, InstallerManifestError) as exc:
        parser.error(str(exc))
    print(f"Installer managed-file manifest ready: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
