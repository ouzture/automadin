from __future__ import annotations

import hashlib
import json
from email.message import Message
from pathlib import Path, PurePosixPath

import pytest

from tools.generate_release_compliance import (
    _cyclonedx_sbom,
    _declared_license,
    _sha256_canonical_text_file,
    _third_party_notices,
    generate_release_compliance,
)
from tools.verify_release_compliance import verify_release_compliance
from tools.write_release_manifest import build_release_manifest


class FakeDistribution:
    def __init__(
        self,
        root: Path,
        *,
        name: str,
        version: str,
        license_expression: str | None,
        evidence: dict[str, bytes] | None = None,
    ) -> None:
        self.version = version
        self.metadata = Message()
        self.metadata["Name"] = name
        if license_expression is not None:
            self.metadata["License-Expression"] = license_expression
        self._root = root
        self.files = []
        for relative_name, content in (evidence or {}).items():
            path = PurePosixPath(relative_name)
            target = root.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            self.files.append(path)

    def locate_file(self, path: object) -> Path:
        return self._root.joinpath(*PurePosixPath(str(path)).parts)


def _write_inputs(
    root: Path,
    *,
    application_license: bool = False,
) -> tuple[Path, Path, Path, dict[str, FakeDistribution]]:
    project = root / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\nname = 'automadin'\nversion = '9.8.7'\n",
        encoding="utf-8",
    )
    lock = project / "release.txt"
    lock.write_text("Alpha==1.0\nBeta_Build==2.0\n", encoding="utf-8")
    app_policy: dict[str, str | None] = {
        "license_expression": None,
        "license_file": None,
    }
    if application_license:
        (project / "LICENSE").write_bytes(b"Exact project license bytes.\n")
        app_policy = {
            "license_expression": "MIT",
            "license_file": "LICENSE",
        }
    policy = project / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "application": app_policy,
                "packages": {
                    "alpha": {
                        "evidence_required": True,
                        "license_expression": "MIT",
                        "scope": "runtime",
                    },
                    "beta-build": {
                        "evidence_note": "Upstream wheel contains metadata only.",
                        "evidence_required": False,
                        "license_expression": "Apache-2.0",
                        "scope": "build",
                    },
                },
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    packages = root / "packages"
    distributions = {
        "alpha": FakeDistribution(
            packages / "alpha",
            name="Alpha",
            version="1.0",
            license_expression="MIT",
            evidence={
                "alpha-1.0.dist-info/licenses/LICENSE": b"Exact alpha license.\r\n",
                "alpha-1.0.dist-info/licenses/NOTICE": b"Exact alpha notice.\n",
            },
        ),
        "beta-build": FakeDistribution(
            packages / "beta",
            name="Beta_Build",
            version="2.0",
            license_expression="Apache-2.0",
        ),
    }
    return project, lock, policy, distributions


def test_canonical_text_hash_normalizes_only_newline_encoding(tmp_path: Path) -> None:
    hashes: list[str] = []
    for index, content in enumerate(
        (b"alpha\nbeta\n", b"alpha\r\nbeta\r\n", b"alpha\rbeta\r")
    ):
        path = tmp_path / f"newlines-{index}.txt"
        path.write_bytes(content)
        hashes.append(_sha256_canonical_text_file(path))

    no_final_newline = tmp_path / "no-final-newline.txt"
    no_final_newline.write_bytes(b"alpha\nbeta")
    changed_whitespace = tmp_path / "changed-whitespace.txt"
    changed_whitespace.write_bytes(b"alpha \nbeta\n")

    assert len(set(hashes)) == 1
    assert _sha256_canonical_text_file(no_final_newline) != hashes[0]
    assert _sha256_canonical_text_file(changed_whitespace) != hashes[0]


def _provider(distributions: dict[str, FakeDistribution]):
    def provide(name: str) -> FakeDistribution:
        return distributions[name.replace("_", "-").casefold()]

    return provide


def _trusted_kwargs(
    project: Path,
    lock: Path,
    policy: Path,
    distributions: dict[str, FakeDistribution],
) -> dict[str, object]:
    return {
        "project_root": project,
        "lock_path": lock,
        "policy_path": policy,
        "distribution_provider": _provider(distributions),
    }


def _release_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_generation_is_deterministic_and_copies_evidence_byte_for_byte(
    tmp_path: Path,
) -> None:
    project, lock, policy, distributions = _write_inputs(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    generate_release_compliance(
        first,
        project,
        lock,
        policy,
        distribution_provider=_provider(distributions),
    )
    generate_release_compliance(
        second,
        project,
        lock,
        policy,
        distribution_provider=_provider(distributions),
    )

    assert _release_bytes(first) == _release_bytes(second)
    manifest = json.loads(
        (first / "third-party-licenses" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["publication_blockers"][0]["code"] == "application-license-missing"
    assert [item["canonical_name"] for item in manifest["components"]] == [
        "alpha",
        "beta-build",
    ]
    evidence = manifest["components"][0]["license_files"][0]
    evidence_path = first.joinpath(*PurePosixPath(evidence["path"]).parts)
    assert evidence_path.read_bytes() == b"Exact alpha license.\r\n"
    assert evidence["sha256"] == hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    sbom = json.loads((first / "release-sbom.cdx.json").read_text(encoding="utf-8"))
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.6"
    assert {component["version"] for component in sbom["components"]} == {"1.0", "2.0"}


def test_declared_license_rejects_missing_and_conflicting_metadata() -> None:
    missing = Message()
    with pytest.raises(ValueError, match="no unambiguous"):
        _declared_license(missing)

    conflicting = Message()
    conflicting["License"] = "MIT"
    conflicting["Classifier"] = (
        "License :: OSI Approved :: Apache Software License"
    )
    with pytest.raises(ValueError, match="conflicting"):
        _declared_license(conflicting)


def test_generation_rejects_version_license_and_evidence_drift(tmp_path: Path) -> None:
    project, lock, policy, distributions = _write_inputs(tmp_path)
    release = tmp_path / "release"
    release.mkdir()

    distributions["alpha"].version = "1.1"
    with pytest.raises(ValueError, match="must be 1.0"):
        generate_release_compliance(
            release,
            project,
            lock,
            policy,
            distribution_provider=_provider(distributions),
        )
    distributions["alpha"].version = "1.0"
    distributions["alpha"].metadata.replace_header("License-Expression", "BSD-3-Clause")
    with pytest.raises(ValueError, match="license changed"):
        generate_release_compliance(
            release,
            project,
            lock,
            policy,
            distribution_provider=_provider(distributions),
        )
    distributions["alpha"].metadata.replace_header("License-Expression", "MIT")
    distributions["alpha"].files = []
    with pytest.raises(ValueError, match="no packaged license"):
        generate_release_compliance(
            release,
            project,
            lock,
            policy,
            distribution_provider=_provider(distributions),
        )


def test_application_license_is_an_explicit_publishability_boundary(
    tmp_path: Path,
) -> None:
    project, lock, policy, distributions = _write_inputs(tmp_path)
    release = tmp_path / "release"
    release.mkdir()
    generate_release_compliance(
        release,
        project,
        lock,
        policy,
        distribution_provider=_provider(distributions),
    )
    release_manifest = build_release_manifest(project, release)

    assert verify_release_compliance(release, release_manifest) == []
    assert any(
        "application-license-missing" in error
        for error in verify_release_compliance(
            release,
            release_manifest,
            require_compliance_ready=True,
            **_trusted_kwargs(project, lock, policy, distributions),
        )
    )


def test_compliance_readiness_requires_trusted_sources(tmp_path: Path) -> None:
    project, lock, policy, distributions = _write_inputs(tmp_path)
    release = tmp_path / "release"
    release.mkdir()
    generate_release_compliance(
        release,
        project,
        lock,
        policy,
        distribution_provider=_provider(distributions),
    )
    release_manifest = build_release_manifest(project, release)

    errors = verify_release_compliance(
        release,
        release_manifest,
        require_compliance_ready=True,
    )

    assert any("readiness requires trusted" in error.lower() for error in errors)


def test_declared_application_license_is_copied_and_compliance_ready(
    tmp_path: Path,
) -> None:
    project, lock, policy, distributions = _write_inputs(
        tmp_path,
        application_license=True,
    )
    release = tmp_path / "release"
    release.mkdir()
    generate_release_compliance(
        release,
        project,
        lock,
        policy,
        distribution_provider=_provider(distributions),
    )
    release_manifest = build_release_manifest(project, release)
    (project / "LICENSE").write_bytes(b"Exact project license bytes.\r\n")

    assert (release / "LICENSE").read_bytes() == b"Exact project license bytes.\n"
    assert (
        verify_release_compliance(
            release,
            release_manifest,
            require_compliance_ready=True,
            **_trusted_kwargs(project, lock, policy, distributions),
        )
        == []
    )


def test_compliance_verification_detects_tampered_evidence(tmp_path: Path) -> None:
    project, lock, policy, distributions = _write_inputs(tmp_path)
    release = tmp_path / "release"
    release.mkdir()
    generate_release_compliance(
        release,
        project,
        lock,
        policy,
        distribution_provider=_provider(distributions),
    )
    release_manifest = build_release_manifest(project, release)
    evidence = next(
        path
        for path in (release / "third-party-licenses").rglob("*")
        if path.is_file() and path.name == "LICENSE"
    )
    evidence.write_bytes(b"tampered")

    assert any(
        "evidence" in error.lower() and "mismatch" in error.lower()
        for error in verify_release_compliance(release, release_manifest)
    )


def test_trusted_verification_rejects_omitted_locked_component(tmp_path: Path) -> None:
    project, lock, policy, distributions = _write_inputs(tmp_path)
    release = tmp_path / "release"
    release.mkdir()
    generate_release_compliance(
        release,
        project,
        lock,
        policy,
        distribution_provider=_provider(distributions),
    )
    license_manifest_path = release / "third-party-licenses" / "manifest.json"
    license_manifest = json.loads(license_manifest_path.read_text(encoding="utf-8"))
    license_manifest["components"] = license_manifest["components"][:1]
    license_manifest_path.write_text(
        json.dumps(license_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    release_manifest = build_release_manifest(project, release)

    errors = verify_release_compliance(
        release,
        release_manifest,
        **_trusted_kwargs(project, lock, policy, distributions),
    )

    assert any("components do not match the trusted release lock" in error for error in errors)


def test_trusted_verification_rejects_coherently_omitted_packaged_evidence(
    tmp_path: Path,
) -> None:
    project, lock, policy, distributions = _write_inputs(tmp_path)
    release = tmp_path / "release"
    release.mkdir()
    generate_release_compliance(
        release,
        project,
        lock,
        policy,
        distribution_provider=_provider(distributions),
    )
    license_manifest_path = release / "third-party-licenses" / "manifest.json"
    license_manifest = json.loads(license_manifest_path.read_text(encoding="utf-8"))
    alpha = license_manifest["components"][0]
    omitted = alpha["license_files"].pop()
    release.joinpath(*PurePosixPath(omitted["path"]).parts).unlink()
    license_manifest_path.write_text(
        json.dumps(license_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    blockers = license_manifest["publication_blockers"]
    application = {
        key: value
        for key, value in license_manifest["application"].items()
        if key not in {"name", "version"}
    }
    (release / "THIRD-PARTY-NOTICES.txt").write_text(
        _third_party_notices(
            "9.8.7",
            application,
            blockers,
            license_manifest["components"],
        ),
        encoding="utf-8",
        newline="\n",
    )
    (release / "release-sbom.cdx.json").write_text(
        json.dumps(
            _cyclonedx_sbom(
                "9.8.7",
                application,
                blockers,
                license_manifest["components"],
            ),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    release_manifest = build_release_manifest(project, release)

    errors = verify_release_compliance(
        release,
        release_manifest,
        **_trusted_kwargs(project, lock, policy, distributions),
    )

    assert any("installed locked distribution metadata" in error for error in errors)


def test_trusted_verification_rejects_falsified_distribution_metadata(
    tmp_path: Path,
) -> None:
    project, lock, policy, distributions = _write_inputs(tmp_path)
    release = tmp_path / "release"
    release.mkdir()
    generate_release_compliance(
        release,
        project,
        lock,
        policy,
        distribution_provider=_provider(distributions),
    )
    license_manifest_path = release / "third-party-licenses" / "manifest.json"
    license_manifest = json.loads(license_manifest_path.read_text(encoding="utf-8"))
    alpha = license_manifest["components"][0]
    alpha["license_metadata"]["source"] = "Classifier"
    alpha["source_urls"] = [{"label": "Source", "url": "https://invalid.example"}]
    license_manifest_path.write_text(
        json.dumps(license_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    release_manifest = build_release_manifest(project, release)

    errors = verify_release_compliance(
        release,
        release_manifest,
        **_trusted_kwargs(project, lock, policy, distributions),
    )

    assert any("installed locked distribution metadata" in error for error in errors)


def test_trusted_readiness_cannot_bypass_removed_blockers(tmp_path: Path) -> None:
    project, lock, policy, distributions = _write_inputs(tmp_path)
    release = tmp_path / "release"
    release.mkdir()
    generate_release_compliance(
        release,
        project,
        lock,
        policy,
        distribution_provider=_provider(distributions),
    )
    license_manifest_path = release / "third-party-licenses" / "manifest.json"
    license_manifest = json.loads(license_manifest_path.read_text(encoding="utf-8"))
    fake_license = release / "LICENSE"
    fake_license.write_text("Not a trusted project license.\n", encoding="utf-8")
    license_manifest["application"] = {
        "license_expression": "MIT",
        "license_file": "LICENSE",
        "name": "Automadin",
        "sha256": hashlib.sha256(fake_license.read_bytes()).hexdigest(),
        "status": "declared",
        "version": "9.8.7",
    }
    license_manifest["publication_blockers"] = []
    license_manifest_path.write_text(
        json.dumps(license_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    sbom_path = release / "release-sbom.cdx.json"
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    for prop in sbom["metadata"]["component"]["properties"]:
        if prop["name"] == "automadin:publication-readiness":
            prop["value"] = "ready"
    sbom_path.write_text(
        json.dumps(sbom, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    release_manifest = build_release_manifest(project, release)

    errors = verify_release_compliance(
        release,
        release_manifest,
        require_compliance_ready=True,
        **_trusted_kwargs(project, lock, policy, distributions),
    )

    assert any("publication blockers do not match trusted policy" in error for error in errors)
    assert any("Publication blocker [application-license-missing]" in error for error in errors)


def test_trusted_verification_rejects_input_hash_and_package_version_falsification(
    tmp_path: Path,
) -> None:
    project, lock, policy, distributions = _write_inputs(tmp_path)
    release = tmp_path / "release"
    release.mkdir()
    generate_release_compliance(
        release,
        project,
        lock,
        policy,
        distribution_provider=_provider(distributions),
    )
    license_manifest_path = release / "third-party-licenses" / "manifest.json"
    license_manifest = json.loads(license_manifest_path.read_text(encoding="utf-8"))
    license_manifest["inputs"]["release_lock"]["sha256"] = "0" * 64
    license_manifest["components"][0]["version"] = "999"
    license_manifest_path.write_text(
        json.dumps(license_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    release_manifest = build_release_manifest(project, release)

    errors = verify_release_compliance(
        release,
        release_manifest,
        **_trusted_kwargs(project, lock, policy, distributions),
    )

    assert any("inputs do not match the trusted lock and policy" in error for error in errors)
    assert any("alpha does not match trusted lock/policy identity" in error for error in errors)


def test_trusted_input_identity_is_independent_of_checkout_newlines(
    tmp_path: Path,
) -> None:
    project, lock, policy, distributions = _write_inputs(tmp_path)
    policy.write_bytes(
        (
            json.dumps(
                json.loads(policy.read_text(encoding="utf-8")),
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    )
    lock.write_bytes(lock.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
    lf_release = tmp_path / "release-lf"
    lf_release.mkdir()
    generate_release_compliance(
        lf_release,
        project,
        lock,
        policy,
        distribution_provider=_provider(distributions),
    )
    lf_manifest = json.loads(
        (lf_release / "third-party-licenses" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    for path in (lock, policy):
        lf_bytes = path.read_bytes()
        assert b"\n" in lf_bytes
        assert b"\r\n" not in lf_bytes
        path.write_bytes(lf_bytes.replace(b"\n", b"\r\n"))

    crlf_release = tmp_path / "release-crlf"
    crlf_release.mkdir()
    generate_release_compliance(
        crlf_release,
        project,
        lock,
        policy,
        distribution_provider=_provider(distributions),
    )
    crlf_manifest = json.loads(
        (crlf_release / "third-party-licenses" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert crlf_manifest["inputs"] == lf_manifest["inputs"]
    assert crlf_manifest["schema_version"] == 2
    assert {
        record["canonicalization"]
        for record in crlf_manifest["inputs"].values()
    } == {"utf-8-lf"}
    for relative in (
        "third-party-licenses/manifest.json",
        "THIRD-PARTY-NOTICES.txt",
        "release-sbom.cdx.json",
    ):
        assert (crlf_release / relative).read_bytes() == (
            lf_release / relative
        ).read_bytes()
    assert (
        verify_release_compliance(
            lf_release,
            build_release_manifest(project, lf_release),
            **_trusted_kwargs(project, lock, policy, distributions),
        )
        == []
    )

    policy_bytes = policy.read_bytes()
    policy.write_bytes(
        policy_bytes.replace(b'  "application"', b'   "application"', 1)
    )
    errors = verify_release_compliance(
        lf_release,
        build_release_manifest(project, lf_release),
        **_trusted_kwargs(project, lock, policy, distributions),
    )

    assert any("inputs do not match the trusted lock and policy" in error for error in errors)


def test_trusted_verification_rejects_falsified_release_manifest_build_version(
    tmp_path: Path,
) -> None:
    project, lock, policy, distributions = _write_inputs(tmp_path)
    release = tmp_path / "release"
    release.mkdir()
    generate_release_compliance(
        release,
        project,
        lock,
        policy,
        distribution_provider=_provider(distributions),
    )
    release_manifest = build_release_manifest(project, release)
    release_manifest["build"]["packages"] = {"Alpha": "999"}

    errors = verify_release_compliance(
        release,
        release_manifest,
        **_trusted_kwargs(project, lock, policy, distributions),
    )

    assert any(
        "build version differs from trusted lock for Alpha" in error for error in errors
    )


def test_trusted_verification_requires_exact_notices(tmp_path: Path) -> None:
    project, lock, policy, distributions = _write_inputs(tmp_path)
    release = tmp_path / "release"
    release.mkdir()
    generate_release_compliance(
        release,
        project,
        lock,
        policy,
        distribution_provider=_provider(distributions),
    )
    notices = release / "THIRD-PARTY-NOTICES.txt"
    notices.write_text(
        notices.read_text(encoding="utf-8").replace("Declared license: MIT", "Declared license: GPL"),
        encoding="utf-8",
        newline="\n",
    )
    release_manifest = build_release_manifest(project, release)

    errors = verify_release_compliance(
        release,
        release_manifest,
        **_trusted_kwargs(project, lock, policy, distributions),
    )

    assert any("does not exactly match trusted compliance data" in error for error in errors)


def test_regeneration_removes_hash_matching_stale_application_license(
    tmp_path: Path,
) -> None:
    project, lock, policy, distributions = _write_inputs(
        tmp_path,
        application_license=True,
    )
    release = tmp_path / "release"
    release.mkdir()
    generate_release_compliance(
        release,
        project,
        lock,
        policy,
        distribution_provider=_provider(distributions),
    )
    policy_value = json.loads(policy.read_text(encoding="utf-8"))
    policy_value["application"] = {"license_expression": None, "license_file": None}
    policy.write_text(json.dumps(policy_value), encoding="utf-8")

    generate_release_compliance(
        release,
        project,
        lock,
        policy,
        distribution_provider=_provider(distributions),
    )

    assert not (release / "LICENSE").exists()


def test_regeneration_rejects_modified_stale_application_license(
    tmp_path: Path,
) -> None:
    project, lock, policy, distributions = _write_inputs(
        tmp_path,
        application_license=True,
    )
    release = tmp_path / "release"
    release.mkdir()
    generate_release_compliance(
        release,
        project,
        lock,
        policy,
        distribution_provider=_provider(distributions),
    )
    (release / "LICENSE").write_text("modified\n", encoding="utf-8")
    policy_value = json.loads(policy.read_text(encoding="utf-8"))
    policy_value["application"] = {"license_expression": None, "license_file": None}
    policy.write_text(json.dumps(policy_value), encoding="utf-8")

    with pytest.raises(ValueError, match="possible stale application license"):
        generate_release_compliance(
            release,
            project,
            lock,
            policy,
            distribution_provider=_provider(distributions),
        )
