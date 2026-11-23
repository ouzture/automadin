import sqlite3
import struct
import json
import hashlib
from pathlib import Path

import pytest

from tools.installer_manifest import MANIFEST_NAME, write_installer_manifest
from tools.verify_release import verify_release


def _write_minimal_pe(path: Path, *, subsystem: int = 2) -> None:
    """Write a small, structurally valid PE32+ fixture (not executable code)."""
    pe_offset = 0x80
    optional_header_size = 0xF0
    content = bytearray(512)
    content[:2] = b"MZ"
    struct.pack_into("<I", content, 0x3C, pe_offset)
    content[pe_offset : pe_offset + 4] = b"PE\x00\x00"
    struct.pack_into(
        "<HHIIIHH",
        content,
        pe_offset + 4,
        0x8664,
        1,
        0,
        0,
        0,
        optional_header_size,
        0x0002,
    )
    struct.pack_into("<H", content, pe_offset + 24, 0x020B)
    struct.pack_into("<H", content, pe_offset + 24 + 68, subsystem)
    path.write_bytes(content)


def _write_minimal_compliance(root: Path) -> dict[str, dict[str, object]]:
    license_root = root / "third-party-licenses"
    evidence = license_root / "example" / "example-1.0.dist-info" / "LICENSE"
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(b"Example license evidence.\n")
    purl = "pkg:pypi/example@1.0"
    blocker = {
        "code": "application-license-missing",
        "message": "A project license decision is required.",
    }
    component = {
        "canonical_name": "example",
        "license_expression": "MIT",
        "license_files": [
            {
                "path": evidence.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                "size": evidence.stat().st_size,
                "source_distribution_path": "example-1.0.dist-info/LICENSE",
            }
        ],
        "license_metadata": {
            "classifiers": ["License :: OSI Approved :: MIT License"],
            "license": [],
            "license_expression": [],
            "source": "Classifier",
        },
        "name": "example",
        "purl": purl,
        "scope": "runtime",
        "source_urls": [],
        "version": "1.0",
    }
    license_manifest = license_root / "manifest.json"
    license_manifest.write_text(
        json.dumps(
            {
                "application": {
                    "license_expression": None,
                    "license_file": None,
                    "name": "Automadin",
                    "status": "missing",
                    "version": "0.1.0",
                },
                "components": [component],
                "inputs": {},
                "publication_blockers": [blocker],
                "sbom_scope": "exact-locked-python-distributions",
                "schema_version": 2,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    sbom = root / "release-sbom.cdx.json"
    sbom.write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "components": [
                    {
                        "bom-ref": purl,
                        "licenses": [{"expression": "MIT"}],
                        "name": "example",
                        "properties": [],
                        "purl": purl,
                        "type": "library",
                        "version": "1.0",
                    }
                ],
                "dependencies": [
                    {"dependsOn": [purl], "ref": "pkg:pypi/automadin@0.1.0"}
                ],
                "metadata": {
                    "component": {
                        "bom-ref": "pkg:pypi/automadin@0.1.0",
                        "name": "Automadin",
                        "properties": [
                            {
                                "name": "automadin:publication-readiness",
                                "value": "blocked",
                            },
                            {
                                "name": "automadin:sbom-scope",
                                "value": "exact-locked-python-distributions",
                            },
                        ],
                        "type": "application",
                        "version": "0.1.0",
                    }
                },
                "specVersion": "1.6",
                "version": 1,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    notices = root / "THIRD-PARTY-NOTICES.txt"
    notices.write_text(
        "AUTOMADIN THIRD-PARTY NOTICES\n"
        "Application version: 0.1.0\n\n"
        "example 1.0\n",
        encoding="utf-8",
    )

    def artifact_record(path: Path) -> dict[str, object]:
        return {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }

    return {
        "license_manifest": artifact_record(license_manifest),
        "sbom": artifact_record(sbom),
        "third_party_notices": artifact_record(notices),
    }


def _make_minimal_release(root: Path) -> None:
    root.mkdir()
    _write_minimal_pe(root / "Automadin.exe")
    _write_minimal_pe(root / "qt6network.dll")
    qwindows = root / "PySide6" / "qt-plugins" / "platforms"
    qwindows.mkdir(parents=True)
    _write_minimal_pe(qwindows / "qwindows.dll")
    driver = root / "playwright" / "driver" / "package"
    driver.mkdir(parents=True)
    _write_minimal_pe(driver.parent / "node.exe")
    (driver / "cli.js").write_text(
        "#!/usr/bin/env node\nconst cli = require('./lib/cli');\ncli.run();\n",
        encoding="utf-8",
    )
    compliance = _write_minimal_compliance(root)
    (root / "release-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "application": {"name": "Automadin", "version": "0.1.0"},
                "build": {
                    "python_version": "3.14.0",
                    "packages": {
                        "Nuitka": "2.7.11",
                        "PySide6": "6.9.1",
                        "playwright": "1.54.0",
                        "pywin32": "310",
                    },
                },
                "compliance": compliance,
            }
        ),
        encoding="utf-8",
    )
    write_installer_manifest(root)


def test_complete_clean_release_passes(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _make_minimal_release(release)

    assert verify_release(release) == []


def test_missing_installer_file_manifest_is_rejected(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _make_minimal_release(release)
    (release / MANIFEST_NAME).unlink()

    assert any(
        f"{MANIFEST_NAME} is missing" in error for error in verify_release(release)
    )


def test_stale_installer_file_manifest_is_rejected(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _make_minimal_release(release)
    (release / "unlisted.dll").write_bytes(b"new payload")

    assert any(
        f"{MANIFEST_NAME} is stale" in error and "unlisted.dll" in error
        for error in verify_release(release)
    )


def test_console_subsystem_application_is_rejected(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _make_minimal_release(release)
    _write_minimal_pe(release / "Automadin.exe", subsystem=3)

    assert any(
        "subsystem is 3" in error and "Windows GUI" in error
        for error in verify_release(release)
    )


def test_missing_qwindows_platform_plugin_is_rejected(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _make_minimal_release(release)
    (release / "PySide6" / "qt-plugins" / "platforms" / "qwindows.dll").unlink()

    assert any("qwindows.dll" in error for error in verify_release(release))


def test_corrupt_qwindows_platform_plugin_is_rejected(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _make_minimal_release(release)
    (
        release / "PySide6" / "qt-plugins" / "platforms" / "qwindows.dll"
    ).write_bytes(b"placeholder")

    assert any(
        "qwindows.dll is invalid" in error for error in verify_release(release)
    )


def test_missing_release_manifest_is_rejected(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _make_minimal_release(release)
    (release / "release-manifest.json").unlink()

    assert any("release-manifest.json is missing" in error for error in verify_release(release))


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        ("Automadin.exe", b""),
        ("Automadin.exe", b"MZ" + (b"\x00" * 510)),
        ("playwright/driver/node.exe", b"placeholder"),
        ("playwright/driver/node.exe", b"MZ" + (b"\x00" * 510)),
    ],
    ids=[
        "empty-app",
        "missing-app-pe-header",
        "tiny-node-placeholder",
        "missing-node-pe-header",
    ],
)
def test_corrupt_or_truncated_windows_executable_is_rejected(
    tmp_path: Path, relative_path: str, content: bytes
) -> None:
    release = tmp_path / "release"
    _make_minimal_release(release)
    (release / relative_path).write_bytes(content)

    errors = verify_release(release)

    file_name = Path(relative_path).name
    assert any(file_name in error and "invalid" in error for error in errors)


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"placeholder content that is not a JavaScript module",
        b"#!/usr/bin/env node\n" + (b"\x00" * 32),
        b"\xff" * 64,
    ],
    ids=["empty", "plain-placeholder", "binary-nul", "invalid-utf8"],
)
def test_empty_or_implausible_playwright_cli_is_rejected(
    tmp_path: Path, content: bytes
) -> None:
    release = tmp_path / "release"
    _make_minimal_release(release)
    (release / "playwright" / "driver" / "package" / "cli.js").write_bytes(
        content
    )

    errors = verify_release(release)

    assert any("cli.js is invalid" in error for error in errors)


def test_database_and_profiles_are_rejected(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _make_minimal_release(release)
    (release / "automadin.db").touch()
    (release / "profiles").mkdir()

    errors = verify_release(release)

    assert any("database" in error.lower() for error in errors)
    assert any("user-data" in error.lower() for error in errors)


def test_missing_qt_network_runtime_is_rejected(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _make_minimal_release(release)
    (release / "qt6network.dll").unlink()

    errors = verify_release(release)

    assert any("Qt Network runtime is missing" in error for error in errors)


@pytest.mark.parametrize(
    "file_name",
    [
        "cache.db",
        "cache.db-journal",
        "state.sqlite",
        "state.sqlite3-wal",
        "automadin-backup-20260716.db",
        "automadin-before-import-20260716.db",
        ".automadin-import-pending.db.123.tmp",
        "automadin-import-rejected-20260716.db",
        "workspace.automadin-bundle",
        ".automadin-portability-pending.json",
        "automadin-portability-rejected-a1b2c3.json",
        "automadin-portability-orphaned-a1b2c3.db",
        ".automadin-portability-staging-a1b2c3.db",
    ],
)
def test_sqlite_and_automadin_transfer_file_names_are_rejected(
    tmp_path: Path, file_name: str
) -> None:
    release = tmp_path / "release"
    _make_minimal_release(release)
    (release / file_name).touch()

    errors = verify_release(release)

    assert any(file_name in error and "by name" in error for error in errors)


def test_renamed_sqlite_database_is_rejected_by_content(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _make_minimal_release(release)
    disguised_database = release / "innocent.bin"
    with sqlite3.connect(disguised_database) as connection:
        connection.execute("CREATE TABLE private_data (value TEXT)")

    errors = verify_release(release)

    assert any("SQLite database content" in error and "innocent.bin" in error for error in errors)


def test_renamed_sqlite_wal_is_rejected_by_content(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _make_minimal_release(release)
    disguised_wal = release / "innocent.bin"
    disguised_wal.write_bytes(b"\x37\x7f\x06\x82" + (b"\x00" * 32))

    errors = verify_release(release)

    assert any("SQLite write-ahead log content" in error for error in errors)


def test_renamed_sqlite_rollback_journal_is_rejected_by_content(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _make_minimal_release(release)
    disguised_journal = release / "innocent.bin"
    disguised_journal.write_bytes(b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7" + (b"\x00" * 32))

    errors = verify_release(release)

    assert any("SQLite rollback journal content" in error for error in errors)


@pytest.mark.parametrize(
    "directory_name",
    [
        "data-backup",
        ".profiles-pending",
        "profiles-rejected-2",
        ".creating-00000000-0000-4000-8000-000000000000-"
        "0123456789abcdef0123456789abcdef",
        ".importing-00000000-0000-4000-8000-000000000000-"
        "0123456789abcdef0123456789abcdef",
        ".deleting-00000000-0000-4000-8000-000000000000-"
        "0123456789abcdef0123456789abcdef",
    ],
)
def test_user_data_staging_directories_are_rejected(
    tmp_path: Path, directory_name: str
) -> None:
    release = tmp_path / "release"
    _make_minimal_release(release)
    (release / directory_name).mkdir()

    errors = verify_release(release)

    assert any(directory_name in error and "user-data directory" in error for error in errors)


def test_bundled_adb_and_browsers_are_rejected(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _make_minimal_release(release)
    (release / "browsers").mkdir()
    (release / "tools" / "platform-tools").mkdir(parents=True)

    errors = verify_release(release)

    assert any("browser binaries" in error for error in errors)
    assert any("Platform Tools" in error for error in errors)


def test_stale_generated_executable_is_rejected(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _make_minimal_release(release)
    (release / "main.exe").touch()

    assert any("main.exe" in error for error in verify_release(release))
