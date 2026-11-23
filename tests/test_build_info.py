from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from automadin import build_info


def _missing_distribution(_name: str) -> str:
    raise PackageNotFoundError


@pytest.fixture
def app_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(build_info, "application_dir", lambda: tmp_path)
    return tmp_path


def test_packaged_version_comes_from_valid_adjacent_manifest(
    monkeypatch: pytest.MonkeyPatch,
    app_dir: Path,
) -> None:
    (app_dir / build_info.RELEASE_MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "application": {"name": "Automadin", "version": "0.2.0"},
                "build": {"python_version": "3.11.9", "packages": {}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        build_info,
        "metadata_version",
        lambda _name: pytest.fail("manifest should take precedence"),
    )

    assert build_info.packaged_app_version("Development build") == "0.2.0"


@pytest.mark.parametrize(
    "manifest",
    [
        "{not-json",
        json.dumps([]),
        json.dumps({"schema_version": 2, "application": {}}),
        json.dumps(
            {
                "schema_version": 1,
                "application": {"name": "Another App", "version": "0.2.0"},
            }
        ),
        json.dumps(
            {
                "schema_version": 1,
                "application": {"name": "Automadin", "version": " 0.2.0 "},
            }
        ),
    ],
)
def test_malformed_or_untrusted_manifest_falls_back_to_distribution_metadata(
    monkeypatch: pytest.MonkeyPatch,
    app_dir: Path,
    manifest: str,
) -> None:
    (app_dir / build_info.RELEASE_MANIFEST_NAME).write_text(manifest, encoding="utf-8")
    monkeypatch.setattr(build_info, "metadata_version", lambda _name: "0.1.9")

    assert build_info.packaged_app_version("development") == "0.1.9"


def test_missing_manifest_and_distribution_use_supplied_development_label(
    monkeypatch: pytest.MonkeyPatch,
    app_dir: Path,
) -> None:
    monkeypatch.setattr(build_info, "metadata_version", _missing_distribution)

    assert build_info.packaged_app_version("Development build") == "Development build"
