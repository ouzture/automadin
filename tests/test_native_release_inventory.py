from __future__ import annotations

import json
from email.message import Message
from pathlib import Path, PurePosixPath

import pytest

from tools.generate_release_compliance import generate_release_compliance
from tools.native_release_inventory import (
    EVIDENCE_DIRECTORY,
    INVENTORY_NAME,
    generate_native_inventory,
    verify_native_inventory,
)
from tools.verify_release_compliance import verify_release_compliance
from tools.write_release_manifest import build_release_manifest


class FakeDistribution:
    version = "1.0"

    def __init__(self, root: Path) -> None:
        self.metadata = Message()
        self.metadata["Name"] = "Alpha"
        self.metadata["License-Expression"] = "MIT"
        evidence = PurePosixPath("alpha-1.0.dist-info/licenses/LICENSE")
        target = root.joinpath(*evidence.parts)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"Exact Alpha license.\n")
        self.files = [evidence]
        self._root = root

    def locate_file(self, path: object) -> Path:
        return self._root.joinpath(*PurePosixPath(str(path)).parts)


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\nname = 'automadin'\nversion = '4.5.6'\n",
        encoding="utf-8",
    )
    lock = project / "release.txt"
    lock.write_text("Alpha==1.0\n", encoding="utf-8")
    native_policy = project / "native-policy.json"
    native_policy.write_text(
        json.dumps(
            {
                "components": [
                    {
                        "id": "automadin",
                        "license_expression": "GPL-3.0-only",
                        "patterns": ["Automadin.exe"],
                        "version_source": "application",
                    }
                ],
                "fulfilled_blocker": "native-and-installer-sbom-incomplete",
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    license_policy = project / "license-policy.json"
    license_policy.write_text(
        json.dumps(
            {
                "application": {
                    "license_expression": None,
                    "license_file": None,
                },
                "packages": {
                    "alpha": {
                        "evidence_required": True,
                        "license_expression": "MIT",
                        "scope": "runtime",
                    }
                },
                "publication_blockers": [
                    {
                        "code": "native-and-installer-sbom-incomplete",
                        "message": "Native inventory required.",
                    }
                ],
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    release = tmp_path / "release"
    release.mkdir()
    (release / "Automadin.exe").write_bytes(b"MZ" + b"application" * 100)
    inno = tmp_path / "ISCC.exe"
    inno.write_bytes(b"MZ" + b"compiler" * 100)
    inno_license = tmp_path / "license.txt"
    inno_license.write_text("Exact Inno Setup license.\n", encoding="utf-8")
    return project, lock, native_policy, license_policy, release, inno


def test_native_inventory_classifies_and_hashes_every_binary(tmp_path: Path) -> None:
    project, lock, native_policy, _license_policy, release, inno = _inputs(tmp_path)
    inno_license = tmp_path / "license.txt"

    destination = generate_native_inventory(
        release,
        project,
        lock,
        native_policy,
        python_version="3.11.4",
        inno_compiler=inno,
        inno_version="6.7.3",
        inno_license=inno_license,
    )
    inventory = verify_native_inventory(
        release,
        project,
        lock,
        native_policy,
        inno_compiler=inno,
    )

    assert destination == release / INVENTORY_NAME
    assert inventory["components"][0]["files"][0]["path"] == "Automadin.exe"
    assert (release / EVIDENCE_DIRECTORY / "Inno-Setup-LICENSE.txt").is_file()

    policy_value = json.loads(native_policy.read_text(encoding="utf-8"))
    native_policy.write_text(json.dumps(policy_value, indent=4), encoding="utf-8")
    assert (
        verify_native_inventory(
            release,
            project,
            lock,
            native_policy,
            inno_compiler=inno,
        )
        == inventory
    )


def test_native_inventory_rejects_unknown_and_tampered_binaries(tmp_path: Path) -> None:
    project, lock, native_policy, _license_policy, release, inno = _inputs(tmp_path)
    (release / "unknown.dll").write_bytes(b"unknown")
    with pytest.raises(ValueError, match="not classified"):
        generate_native_inventory(
            release,
            project,
            lock,
            native_policy,
            python_version="3.11.4",
            inno_compiler=inno,
            inno_version="6.7.3",
            inno_license=tmp_path / "license.txt",
        )

    (release / "unknown.dll").unlink()
    generate_native_inventory(
        release,
        project,
        lock,
        native_policy,
        python_version="3.11.4",
        inno_compiler=inno,
        inno_version="6.7.3",
        inno_license=tmp_path / "license.txt",
    )
    (release / "Automadin.exe").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="failed verification"):
        verify_native_inventory(
            release,
            project,
            lock,
            native_policy,
            inno_compiler=inno,
        )


def test_compliance_clears_native_blocker_only_for_trusted_inventory(
    tmp_path: Path,
) -> None:
    project, lock, native_policy, license_policy, release, inno = _inputs(tmp_path)
    generate_native_inventory(
        release,
        project,
        lock,
        native_policy,
        python_version="3.11.4",
        inno_compiler=inno,
        inno_version="6.7.3",
        inno_license=tmp_path / "license.txt",
    )
    distribution = FakeDistribution(tmp_path / "distribution")
    generate_release_compliance(
        release,
        project,
        lock,
        license_policy,
        native_policy_path=native_policy,
        inno_compiler=inno,
        distribution_provider=lambda _name: distribution,
    )
    release_manifest = build_release_manifest(project, release)
    license_manifest = json.loads(
        (release / "third-party-licenses" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert [
        blocker["code"] for blocker in license_manifest["publication_blockers"]
    ] == ["application-license-missing"]
    assert license_manifest["native_inventory"]["fulfilled_blocker"] == (
        "native-and-installer-sbom-incomplete"
    )
    assert (
        verify_release_compliance(
            release,
            release_manifest,
            project_root=project,
            lock_path=lock,
            policy_path=license_policy,
            native_policy_path=native_policy,
            inno_compiler=inno,
            distribution_provider=lambda _name: distribution,
        )
        == []
    )
