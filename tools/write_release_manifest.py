"""Write deterministic release identity and build-environment metadata."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path

try:
    from tools.generate_release_compliance import (
        LICENSE_DIRECTORY_NAME,
        LICENSE_MANIFEST_NAME,
        NOTICES_NAME,
        SBOM_NAME,
    )
    from tools.project_metadata import read_project_version
except ModuleNotFoundError as exc:
    if exc.name != "tools":
        raise
    from generate_release_compliance import (
        LICENSE_DIRECTORY_NAME,
        LICENSE_MANIFEST_NAME,
        NOTICES_NAME,
        SBOM_NAME,
    )
    from project_metadata import read_project_version


MANIFEST_NAME = "release-manifest.json"
RECORDED_DISTRIBUTIONS = ("Nuitka", "PySide6", "playwright", "pywin32")


def _artifact_record(release_dir: Path, relative_path: str) -> dict[str, object]:
    path = release_dir.joinpath(*relative_path.split("/"))
    if not path.is_file():
        raise ValueError(f"Required compliance artifact does not exist: {relative_path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"path": relative_path, "sha256": digest, "size": path.stat().st_size}


def build_release_manifest(
    project_root: Path,
    release_dir: Path | None = None,
) -> dict[str, object]:
    packages: dict[str, str | None] = {}
    for distribution in RECORDED_DISTRIBUTIONS:
        try:
            packages[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            packages[distribution] = None

    manifest: dict[str, object] = {
        "schema_version": 1,
        "application": {
            "name": "Automadin",
            "version": read_project_version(project_root),
        },
        "build": {
            "python_version": platform.python_version(),
            "packages": packages,
        },
    }
    if release_dir is not None:
        manifest["compliance"] = {
            "license_manifest": _artifact_record(
                release_dir,
                f"{LICENSE_DIRECTORY_NAME}/{LICENSE_MANIFEST_NAME}",
            ),
            "sbom": _artifact_record(release_dir, SBOM_NAME),
            "third_party_notices": _artifact_record(release_dir, NOTICES_NAME),
        }
    return manifest


def write_release_manifest(release_dir: Path, project_root: Path) -> Path:
    if not release_dir.is_dir():
        raise ValueError(f"Release directory does not exist: {release_dir}")
    destination = release_dir / MANIFEST_NAME
    destination.write_text(
        json.dumps(
            build_release_manifest(project_root, release_dir),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--project-root", required=True, type=Path)
    args = parser.parse_args()
    destination = write_release_manifest(
        args.release_dir.resolve(), args.project_root.resolve()
    )
    print(f"Release manifest written: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
