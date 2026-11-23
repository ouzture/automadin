from __future__ import annotations

import hashlib
import io
import json
import subprocess
from email.message import Message
from pathlib import Path, PurePosixPath

import pytest

from tools.generate_release_compliance import generate_release_compliance
from tools.release_sources import (
    MANIFEST_NAME,
    OFFER_NAME,
    REQUIRED_AUTOMADIN_SOURCE_FILES,
    prepare_release_sources,
    verify_release_sources,
)
from tools.verify_release_compliance import verify_release_compliance
from tools.write_release_manifest import build_release_manifest


class FakeDistribution:
    version = "6.10.2"

    def __init__(self, root: Path) -> None:
        self.metadata = Message()
        self.metadata["Name"] = "PySide6"
        self.metadata["License-Expression"] = (
            "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only"
        )
        evidence = PurePosixPath("pyside6-6.10.2.dist-info/licenses/LICENSE")
        target = root.joinpath(*evidence.parts)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"Fake exact PySide license evidence.\n")
        self.files = [evidence]
        self._root = root

    def locate_file(self, path: object) -> Path:
        return self._root.joinpath(*PurePosixPath(str(path)).parts)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _git(project: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(project), *arguments],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _project(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, bytes]]:
    project = tmp_path / "project"
    project.mkdir()
    version = "9.8.7"
    required_content = {
        "LICENSE": "Fake GPL text.\n",
        "pyproject.toml": (
            "[project]\nname = \"automadin\"\nversion = \"9.8.7\"\n"
        ),
        "pysidedeploy.spec": "[app]\n",
        "requirements-release-windows.txt": "PySide6==6.10.2\n",
        "release-license-policy.json": "{}\n",
        "release-native-policy.json": "{}\n",
        "packaging/Automadin.iss": "[Setup]\n",
        "tools/build_windows.ps1": "Write-Host build\n",
    }
    external = {
        "pyside-source.tar.xz": b"exact-pyside-source",
        "qt-source.tar.xz": b"exact-qt-source",
    }
    archives = []
    for identifier, filename in (
        ("qt-for-python", "pyside-source.tar.xz"),
        ("qt", "qt-source.tar.xz"),
    ):
        content = external[filename]
        archives.append(
            {
                "filename": filename,
                "id": identifier,
                "license_expression": "GPL-3.0-only",
                "package": "PySide6",
                "sha256": _sha256(content),
                "size": len(content),
                "url": f"https://download.qt.io/testing/{filename}",
                "version": "6.10.2",
            }
        )
    policy = project / "release-source-policy.json"
    policy.write_text(
        json.dumps(
            {
                "archives": archives,
                "fulfilled_blocker": "qt-gpl-source-distribution-incomplete",
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    for relative, content in required_content.items():
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    assert REQUIRED_AUTOMADIN_SOURCE_FILES.issubset(
        {
            path.relative_to(project).as_posix()
            for path in project.rglob("*")
            if path.is_file()
        }
    )
    _git(project, "init")
    _git(project, "add", ".")
    _git(
        project,
        "-c",
        "user.name=Automadin Tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "-m",
        f"source {version}",
    )
    return (
        project,
        project / "requirements-release-windows.txt",
        policy,
        external,
    )


def _opener(external: dict[str, bytes]):
    def open_url(url: str) -> io.BytesIO:
        return io.BytesIO(external[url.rsplit("/", 1)[-1]])

    return open_url


def test_prepare_and_verify_exact_release_source_set(tmp_path: Path) -> None:
    project, lock, policy, external = _project(tmp_path)
    output = tmp_path / "source"

    outputs = prepare_release_sources(
        project,
        output,
        policy,
        lock,
        download=True,
        opener=_opener(external),
    )
    manifest = verify_release_sources(project, output, policy, lock)

    assert outputs == {
        "manifest": output / MANIFEST_NAME,
        "offer": output / OFFER_NAME,
    }
    assert manifest["application_version"] == "9.8.7"
    assert {artifact["id"] for artifact in manifest["artifacts"]} == {
        "automadin",
        "qt",
        "qt-for-python",
    }
    offer = (output / OFFER_NAME).read_text(encoding="utf-8")
    assert "same release location" in offer
    assert "provenance only" in offer

    policy_value = json.loads(policy.read_text(encoding="utf-8"))
    policy.write_text(json.dumps(policy_value, indent=4), encoding="utf-8")
    assert verify_release_sources(project, output, policy, lock) == manifest


def test_source_verification_rejects_tampering_and_dirty_source(
    tmp_path: Path,
) -> None:
    project, lock, policy, external = _project(tmp_path)
    output = tmp_path / "source"
    prepare_release_sources(
        project,
        output,
        policy,
        lock,
        download=True,
        opener=_opener(external),
    )
    (output / "qt-source.tar.xz").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="failed policy hash"):
        verify_release_sources(project, output, policy, lock)

    with (project / "pyproject.toml").open("a", encoding="utf-8") as pyproject:
        pyproject.write("# tracked change\n")
    with pytest.raises(ValueError, match="no tracked changes"):
        prepare_release_sources(project, tmp_path / "other", policy, lock)


def test_compliance_clears_source_blocker_only_with_verified_archives(
    tmp_path: Path,
) -> None:
    project, lock, source_policy, external = _project(tmp_path)
    source = tmp_path / "source"
    prepare_release_sources(
        project,
        source,
        source_policy,
        lock,
        download=True,
        opener=_opener(external),
    )
    license_policy = project / "policy.json"
    license_policy.write_text(
        json.dumps(
            {
                "application": {
                    "license_expression": None,
                    "license_file": None,
                },
                "packages": {
                    "pyside6": {
                        "evidence_required": True,
                        "license_expression": (
                            "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only"
                        ),
                        "scope": "runtime",
                    }
                },
                "publication_blockers": [
                    {
                        "code": "qt-gpl-source-distribution-incomplete",
                        "message": "Source required.",
                    },
                    {
                        "code": "native-and-installer-sbom-incomplete",
                        "message": "Native inventory required.",
                    },
                ],
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    distribution = FakeDistribution(tmp_path / "distribution")
    release = tmp_path / "release"
    release.mkdir()
    generate_release_compliance(
        release,
        project,
        lock,
        license_policy,
        source_dir=source,
        source_policy_path=source_policy,
        distribution_provider=lambda _name: distribution,
    )
    release_manifest = build_release_manifest(project, release)
    release_manifest["build"]["packages"]["PySide6"] = "6.10.2"
    license_manifest = json.loads(
        (release / "third-party-licenses" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert [
        blocker["code"] for blocker in license_manifest["publication_blockers"]
    ] == ["application-license-missing", "native-and-installer-sbom-incomplete"]
    assert (release / "corresponding-source" / OFFER_NAME).is_file()
    assert (
        verify_release_compliance(
            release,
            release_manifest,
            project_root=project,
            lock_path=lock,
            policy_path=license_policy,
            source_dir=source,
            source_policy_path=source_policy,
            distribution_provider=lambda _name: distribution,
        )
        == []
    )
