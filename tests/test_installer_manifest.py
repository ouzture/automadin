from pathlib import Path

import pytest

from tools.installer_manifest import (
    MANIFEST_HEADER,
    MANIFEST_NAME,
    MAX_MANAGED_FILE_COUNT,
    MAX_MANAGED_PATH_LENGTH,
    InstallerManifestError,
    read_installer_manifest,
    release_file_entries,
    verify_installer_manifest,
    write_installer_manifest,
)


def test_manifest_lists_exact_release_files_and_itself(tmp_path: Path) -> None:
    (tmp_path / "Automadin.exe").write_bytes(b"application")
    nested = tmp_path / "plugins" / "platforms"
    nested.mkdir(parents=True)
    (nested / "qwindows.dll").write_bytes(b"plugin")

    manifest = write_installer_manifest(tmp_path)

    assert manifest == tmp_path / MANIFEST_NAME
    assert not manifest.read_bytes().startswith(b"\xef\xbb\xbf")
    assert read_installer_manifest(manifest) == (
        "Automadin.exe",
        MANIFEST_NAME,
        "plugins/platforms/qwindows.dll",
    )
    assert verify_installer_manifest(tmp_path) == []


def test_manifest_is_reproducible_and_does_not_list_temporary_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "Automadin.exe").write_bytes(b"application")

    manifest = write_installer_manifest(tmp_path)
    first = manifest.read_bytes()
    write_installer_manifest(tmp_path)

    assert manifest.read_bytes() == first
    assert all(".tmp" not in entry for entry in read_installer_manifest(manifest))


def test_verification_reports_unlisted_and_removed_payloads(tmp_path: Path) -> None:
    payload = tmp_path / "old.dll"
    payload.write_bytes(b"old")
    write_installer_manifest(tmp_path)
    payload.unlink()
    (tmp_path / "new.dll").write_bytes(b"new")

    errors = verify_installer_manifest(tmp_path)

    assert len(errors) == 1
    assert "new.dll" in errors[0]
    assert "old.dll" in errors[0]


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../escape.dll",
        "/absolute.dll",
        r"folder\windows-separator.dll",
        "C:/drive.dll",
        "folder/./dot.dll",
        "folder/../parent.dll",
        "nul\0byte.dll",
        " leading.dll",
        "trailing.dll ",
        "folder./file.dll",
        "folder/file?.dll",
        "folder/caf\u00e9.dll",
        "x" * (MAX_MANAGED_PATH_LENGTH + 1),
    ],
)
def test_reader_rejects_unsafe_managed_paths(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    manifest = tmp_path / MANIFEST_NAME
    manifest.write_text(
        f"{MANIFEST_HEADER}\n{MANIFEST_NAME}\n{unsafe_path}\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallerManifestError, match="unsafe managed-file path"):
        read_installer_manifest(manifest)


@pytest.mark.parametrize(
    "protected_path",
    [
        "data/keep.txt",
        "nested/profiles/keep.txt",
        "runtime/browser.bin",
        "logs/app.log",
        "portable.flag",
        "automadin.db",
        "cache.sqlite3-wal",
        "unins000.exe",
    ],
)
def test_reader_rejects_paths_reserved_for_user_data_or_uninstaller(
    tmp_path: Path,
    protected_path: str,
) -> None:
    manifest = tmp_path / MANIFEST_NAME
    manifest.write_text(
        f"{MANIFEST_HEADER}\n{MANIFEST_NAME}\n{protected_path}\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallerManifestError, match="protected user-data path"):
        read_installer_manifest(manifest)


def test_reader_rejects_more_than_installer_entry_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tools.installer_manifest.MAX_MANAGED_FILE_COUNT", 2)
    manifest = tmp_path / MANIFEST_NAME
    manifest.write_text(
        f"{MANIFEST_HEADER}\na.dll\nb.dll\n{MANIFEST_NAME}\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallerManifestError, match="more than 2 managed files"):
        read_installer_manifest(manifest)

    assert MAX_MANAGED_FILE_COUNT == 200_000


def test_reader_rejects_case_insensitive_duplicates(tmp_path: Path) -> None:
    manifest = tmp_path / MANIFEST_NAME
    manifest.write_text(
        f"{MANIFEST_HEADER}\nAutomadin.exe\nautomadin.EXE\n{MANIFEST_NAME}\n",
        encoding="utf-8",
    )

    with pytest.raises(InstallerManifestError, match="duplicate Windows path"):
        read_installer_manifest(manifest)


def test_release_tree_rejects_symlinks_when_supported(tmp_path: Path) -> None:
    source = tmp_path / "source.dll"
    source.write_bytes(b"payload")
    link = tmp_path / "link.dll"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("symbolic links are unavailable")

    with pytest.raises(InstallerManifestError, match="symbolic link"):
        release_file_entries(tmp_path)
