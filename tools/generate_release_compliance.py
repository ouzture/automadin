"""Generate deterministic offline third-party notices and a CycloneDX SBOM."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import shutil
import sys
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

try:
    from tools.native_release_inventory import (
        EVIDENCE_DIRECTORY as NATIVE_EVIDENCE_DIRECTORY,
        INVENTORY_NAME as NATIVE_INVENTORY_NAME,
        INNO_LICENSE_NAME,
        verify_native_inventory,
    )
    from tools.project_metadata import read_project_version
    from tools.release_sources import (
        MANIFEST_NAME as SOURCE_MANIFEST_NAME,
        OFFER_NAME as SOURCE_OFFER_NAME,
        copy_source_offer,
        verify_release_sources,
    )
    from tools.verify_release_environment import read_release_lock
except ModuleNotFoundError as exc:
    if exc.name != "tools":
        raise
    from native_release_inventory import (
        EVIDENCE_DIRECTORY as NATIVE_EVIDENCE_DIRECTORY,
        INVENTORY_NAME as NATIVE_INVENTORY_NAME,
        INNO_LICENSE_NAME,
        verify_native_inventory,
    )
    from project_metadata import read_project_version
    from release_sources import (
        MANIFEST_NAME as SOURCE_MANIFEST_NAME,
        OFFER_NAME as SOURCE_OFFER_NAME,
        copy_source_offer,
        verify_release_sources,
    )
    from verify_release_environment import read_release_lock


SBOM_NAME = "release-sbom.cdx.json"
NOTICES_NAME = "THIRD-PARTY-NOTICES.txt"
LICENSE_DIRECTORY_NAME = "third-party-licenses"
LICENSE_MANIFEST_NAME = "manifest.json"
SBOM_SCOPE = "exact-locked-python-distributions"
SOURCE_OFFER_DIRECTORY_NAME = "corresponding-source"
INPUT_CANONICALIZATION = "utf-8-lf"
MAX_LICENSE_FILE_SIZE = 4 * 1024 * 1024
VALID_SCOPE = {"build", "runtime"}
SPDX_EXPRESSION_PATTERN = re.compile(
    r"^[A-Za-z0-9.+-]+(?:\s+(?:AND|OR|WITH)\s+[A-Za-z0-9.+-]+|[()\s])*$"
)
LICENSE_FILE_NAME_PATTERN = re.compile(
    r"^(?:license|licence|copying|notice|copyright)(?:$|[._-])|"
    r"^third[-_ ]?party[-_ ]?notices?(?:$|[._-])",
    re.IGNORECASE,
)
LEGACY_LICENSE_ALIASES = {
    "Apache 2.0": "Apache-2.0",
    "Apache License, Version 2.0": "Apache-2.0",
    "MIT": "MIT",
    "PSF": "PSF-2.0",
}
LICENSE_CLASSIFIER_ALIASES = {
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
}


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_canonical_text_file(path: Path) -> str:
    """Hash UTF-8 text with universal newlines represented canonically as LF."""
    content = path.read_text(encoding="utf-8")
    return _sha256_bytes(content.encode("utf-8"))


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _source_offer_record(
    release_dir: Path,
    source_manifest: dict[str, Any],
) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for name in (SOURCE_MANIFEST_NAME, SOURCE_OFFER_NAME):
        relative = f"{SOURCE_OFFER_DIRECTORY_NAME}/{name}"
        path = release_dir.joinpath(*PurePosixPath(relative).parts)
        files.append(
            {
                "path": relative,
                "sha256": _sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    return {
        "files": files,
        "fulfilled_blocker": source_manifest["fulfilled_blocker"],
    }


def _native_inventory_record(release_dir: Path, inventory: dict[str, Any]) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for relative in (
        NATIVE_INVENTORY_NAME,
        f"{NATIVE_EVIDENCE_DIRECTORY}/{INNO_LICENSE_NAME}",
    ):
        path = release_dir.joinpath(*PurePosixPath(relative).parts)
        files.append(
            {
                "path": relative,
                "sha256": _sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    return {
        "files": files,
        "fulfilled_blocker": inventory["fulfilled_blocker"],
    }


def _metadata_values(metadata: Any, key: str) -> list[str]:
    values = metadata.get_all(key) or []
    return [str(value).strip() for value in values if str(value).strip()]


def _valid_spdx_expression(expression: str) -> bool:
    return bool(expression and SPDX_EXPRESSION_PATTERN.fullmatch(expression))


def _declared_license(metadata: Any) -> tuple[str, str]:
    expressions = set(_metadata_values(metadata, "License-Expression"))
    if len(expressions) > 1:
        raise ValueError("distribution has conflicting License-Expression values")
    if expressions:
        expression = next(iter(expressions))
        if not _valid_spdx_expression(expression):
            raise ValueError(f"distribution has an invalid SPDX expression: {expression!r}")
        return expression, "License-Expression"

    candidates: set[tuple[str, str]] = set()
    licenses = set(_metadata_values(metadata, "License"))
    for license_value in licenses:
        normalized = LEGACY_LICENSE_ALIASES.get(license_value)
        if normalized is None and _valid_spdx_expression(license_value):
            normalized = license_value
        if normalized:
            candidates.add((normalized, "License"))

    classifiers = {
        value
        for value in _metadata_values(metadata, "Classifier")
        if value.startswith("License ::")
    }
    for classifier in classifiers:
        normalized = LICENSE_CLASSIFIER_ALIASES.get(classifier)
        if normalized:
            candidates.add((normalized, "Classifier"))

    expressions = {expression for expression, _source in candidates}
    if not expressions:
        raise ValueError("distribution has no unambiguous SPDX license declaration")
    if len(expressions) > 1:
        raise ValueError(
            "distribution has conflicting legacy license declarations: "
            + ", ".join(sorted(expressions))
        )
    expression = next(iter(expressions))
    sources = "+".join(
        sorted(source for candidate, source in candidates if candidate == expression)
    )
    return expression, sources


def _read_policy(path: Path) -> dict[str, Any]:
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read license policy {path}: {exc}") from exc
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise ValueError("License policy must be an object with schema_version 1.")
    application = policy.get("application")
    packages = policy.get("packages")
    if not isinstance(application, dict) or not isinstance(packages, dict):
        raise ValueError("License policy must contain application and packages objects.")
    return policy


def _validate_policy(
    policy: dict[str, Any],
    pins: dict[str, tuple[str, str]],
) -> None:
    packages = policy["packages"]
    canonical_policy = {_canonical_name(str(name)): value for name, value in packages.items()}
    if len(canonical_policy) != len(packages):
        raise ValueError("License policy contains duplicate canonical package names.")
    if set(canonical_policy) != set(pins):
        missing = sorted(set(pins) - set(canonical_policy))
        extra = sorted(set(canonical_policy) - set(pins))
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unexpected " + ", ".join(extra))
        raise ValueError("License policy does not match the release lock: " + "; ".join(details))
    for canonical, package_policy in canonical_policy.items():
        if not isinstance(package_policy, dict):
            raise ValueError(f"License policy for {canonical} must be an object.")
        expression = package_policy.get("license_expression")
        if not isinstance(expression, str) or not _valid_spdx_expression(expression):
            raise ValueError(f"License policy for {canonical} has no valid SPDX expression.")
        if package_policy.get("scope") not in VALID_SCOPE:
            raise ValueError(f"License policy for {canonical} has an invalid scope.")
        evidence_required = package_policy.get("evidence_required")
        if not isinstance(evidence_required, bool):
            raise ValueError(
                f"License policy for {canonical} must declare evidence_required."
            )
        if not evidence_required and not str(package_policy.get("evidence_note", "")).strip():
            raise ValueError(
                f"License policy exception for {canonical} requires an evidence_note."
            )
    _policy_blockers(policy)


def _policy_blockers(policy: dict[str, Any]) -> list[dict[str, str]]:
    blockers_value = policy.get("publication_blockers", [])
    if not isinstance(blockers_value, list):
        raise ValueError("License policy publication_blockers must be a list.")
    blockers: list[dict[str, str]] = []
    seen: set[str] = set()
    for blocker in blockers_value:
        if not isinstance(blocker, dict):
            raise ValueError("License policy publication blocker must be an object.")
        code = blocker.get("code")
        message = blocker.get("message")
        if (
            not isinstance(code, str)
            or not code
            or not isinstance(message, str)
            or not message
        ):
            raise ValueError("License policy publication blocker is invalid.")
        if code in seen:
            raise ValueError(f"License policy duplicates publication blocker {code}.")
        seen.add(code)
        blockers.append({"code": code, "message": message})
    return blockers


def _safe_distribution_path(value: object) -> PurePosixPath:
    raw = str(value).replace("\\", "/")
    relative = PurePosixPath(raw)
    if (
        not raw
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or any(":" in part for part in relative.parts)
    ):
        raise ValueError(f"Unsafe distribution file path: {raw!r}")
    return relative


def _is_license_evidence(path: PurePosixPath, declared_files: set[str]) -> bool:
    normalized = path.as_posix().casefold()
    for declared in declared_files:
        declared_normalized = declared.replace("\\", "/").strip("/").casefold()
        if (
            normalized == declared_normalized
            or normalized.endswith("/" + declared_normalized)
            or normalized.endswith("/licenses/" + declared_normalized)
        ):
            return True
    return bool(LICENSE_FILE_NAME_PATTERN.search(path.name))


def _license_evidence(
    distribution: importlib.metadata.Distribution,
    canonical_name: str,
) -> list[tuple[str, bytes]]:
    declared = set(_metadata_values(distribution.metadata, "License-File"))
    found_declared: set[str] = set()
    evidence: list[tuple[str, bytes]] = []
    seen_paths: set[str] = set()
    for package_path in sorted(distribution.files or (), key=lambda value: str(value).casefold()):
        candidate = PurePosixPath(str(package_path).replace("\\", "/"))
        if not _is_license_evidence(candidate, declared):
            continue
        relative = _safe_distribution_path(package_path)
        normalized = relative.as_posix().casefold()
        for declared_path in declared:
            declared_normalized = declared_path.replace("\\", "/").strip("/").casefold()
            if (
                normalized == declared_normalized
                or normalized.endswith("/" + declared_normalized)
                or normalized.endswith("/licenses/" + declared_normalized)
            ):
                found_declared.add(declared_path)
        if normalized in seen_paths:
            continue
        source = Path(distribution.locate_file(package_path))
        if not source.is_file():
            raise ValueError(
                f"{canonical_name} license evidence is missing: {relative.as_posix()}"
            )
        size = source.stat().st_size
        if size <= 0 or size > MAX_LICENSE_FILE_SIZE:
            raise ValueError(
                f"{canonical_name} license evidence has invalid size {size}: "
                f"{relative.as_posix()}"
            )
        content = source.read_bytes()
        if b"\x00" in content:
            raise ValueError(
                f"{canonical_name} license evidence is not text: {relative.as_posix()}"
            )
        try:
            content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"{canonical_name} license evidence is not UTF-8: "
                f"{relative.as_posix()}"
            ) from exc
        seen_paths.add(normalized)
        evidence.append((relative.as_posix(), content))
    missing_declared = declared - found_declared
    if missing_declared:
        raise ValueError(
            f"{canonical_name} metadata names missing License-File entries: "
            + ", ".join(sorted(missing_declared))
        )
    return evidence


def _project_urls(metadata: Any) -> list[dict[str, str]]:
    urls: set[tuple[str, str]] = set()
    for value in _metadata_values(metadata, "Project-URL"):
        label, separator, url = value.partition(",")
        if not separator:
            label, url = "Project", value
        label = label.strip() or "Project"
        url = url.strip()
        if url.startswith(("https://", "http://")):
            urls.add((label, url))
    for value in _metadata_values(metadata, "Home-page"):
        if value.startswith(("https://", "http://")):
            urls.add(("Homepage", value))
    return [{"label": label, "url": url} for label, url in sorted(urls)]


def _application_license(
    project_root: Path,
    policy: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]], tuple[str, bytes] | None]:
    application_policy = policy["application"]
    expression = application_policy.get("license_expression")
    relative_name = application_policy.get("license_file")
    if expression is None and relative_name is None:
        return (
            {"status": "missing", "license_expression": None, "license_file": None},
            [
                {
                    "code": "application-license-missing",
                    "message": (
                        "Automadin has no declared project license or root license file. "
                        "A license decision is required before public distribution."
                    ),
                }
            ],
            None,
        )
    if not isinstance(expression, str) or not _valid_spdx_expression(expression):
        raise ValueError("Application license_expression must be a valid SPDX expression.")
    if not isinstance(relative_name, str):
        raise ValueError("Application license_file must be a relative path.")
    relative = _safe_distribution_path(relative_name)
    if len(relative.parts) != 1:
        raise ValueError("Application license_file must name a project-root file.")
    source = project_root / relative.as_posix()
    if not source.is_file():
        raise ValueError(f"Application license file does not exist: {relative.as_posix()}")
    content = source.read_bytes()
    if not content or b"\x00" in content:
        raise ValueError("Application license file must be non-empty text.")
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Application license file must be UTF-8.") from exc
    content = decoded.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    return (
        {
            "status": "declared",
            "license_expression": expression,
            "license_file": relative.as_posix(),
            "sha256": _sha256_bytes(content),
        },
        [],
        (relative.as_posix(), content),
    )


def _collect_components(
    pins: dict[str, tuple[str, str]],
    policy: dict[str, Any],
    distribution_provider: Callable[[str], importlib.metadata.Distribution],
) -> list[dict[str, Any]]:
    package_policy = {
        _canonical_name(name): value for name, value in policy["packages"].items()
    }
    components: list[dict[str, Any]] = []
    for canonical, (locked_name, expected_version) in sorted(pins.items()):
        try:
            distribution = distribution_provider(locked_name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ValueError(
                f"Locked distribution is not installed: {locked_name}=={expected_version}"
            ) from exc
        actual_name = str(distribution.metadata.get("Name") or "").strip()
        actual_version = str(distribution.version)
        if _canonical_name(actual_name) != canonical:
            raise ValueError(
                f"Distribution identity mismatch for {locked_name}: {actual_name or '<missing>'}"
            )
        if actual_version != expected_version:
            raise ValueError(
                f"{locked_name} must be {expected_version}; found {actual_version}."
            )
        expression, metadata_source = _declared_license(distribution.metadata)
        expected_expression = package_policy[canonical]["license_expression"]
        if expression != expected_expression:
            raise ValueError(
                f"{locked_name} license changed: expected {expected_expression!r}, "
                f"found {expression!r} from {metadata_source}."
            )
        evidence = _license_evidence(distribution, canonical)
        if package_policy[canonical]["evidence_required"] and not evidence:
            raise ValueError(f"{locked_name} has no packaged license/notice evidence.")
        component: dict[str, Any] = {
            "canonical_name": canonical,
            "license_expression": expression,
            "license_metadata": {
                "classifiers": sorted(
                    value
                    for value in _metadata_values(distribution.metadata, "Classifier")
                    if value.startswith("License ::")
                ),
                "license": sorted(_metadata_values(distribution.metadata, "License")),
                "license_expression": sorted(
                    _metadata_values(distribution.metadata, "License-Expression")
                ),
                "source": metadata_source,
            },
            "name": locked_name,
            "purl": f"pkg:pypi/{quote(canonical, safe='.-')}@{quote(actual_version, safe='.+-')}",
            "scope": package_policy[canonical]["scope"],
            "source_urls": _project_urls(distribution.metadata),
            "version": actual_version,
            "_evidence": evidence,
        }
        evidence_note = package_policy[canonical].get("evidence_note")
        if evidence_note:
            component["evidence_note"] = evidence_note
        components.append(component)
    return components


def _serialize_components(
    components: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return deterministic component records, including exact evidence inventory."""
    serialized_components: list[dict[str, Any]] = []
    for component in components:
        serialized = {
            key: value for key, value in component.items() if key != "_evidence"
        }
        entries: list[dict[str, object]] = []
        for source_path, content in component.get("_evidence", ()):
            relative = (
                PurePosixPath(LICENSE_DIRECTORY_NAME)
                / component["canonical_name"]
                / PurePosixPath(source_path)
            )
            entries.append(
                {
                    "path": relative.as_posix(),
                    "sha256": _sha256_bytes(content),
                    "size": len(content),
                    "source_distribution_path": source_path,
                }
            )
        serialized["license_files"] = entries
        serialized_components.append(serialized)
    return serialized_components


def _write_evidence(
    destination: Path,
    components: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    serialized_components = _serialize_components(components)
    for component, serialized in zip(components, serialized_components, strict=True):
        for (source_path, content), evidence in zip(
            component.get("_evidence", ()),
            serialized["license_files"],
            strict=True,
        ):
            if evidence["source_distribution_path"] != source_path:
                raise ValueError("Serialized license evidence source path changed unexpectedly.")
            target = destination.joinpath(*PurePosixPath(evidence["path"]).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    return serialized_components


def _prepare_application_license_output(
    release_dir: Path,
    application_license: dict[str, Any],
) -> None:
    """Remove only hash-matching prior generated licenses; reject ambiguous stale files."""
    current_name = (
        application_license.get("license_file")
        if application_license.get("status") == "declared"
        else None
    )
    prior_hashes: dict[str, str] = {}
    prior_manifest = release_dir / LICENSE_DIRECTORY_NAME / LICENSE_MANIFEST_NAME
    if prior_manifest.is_file():
        try:
            prior_value = json.loads(prior_manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            prior_value = None
        prior_application = (
            prior_value.get("application") if isinstance(prior_value, dict) else None
        )
        if (
            isinstance(prior_application, dict)
            and prior_application.get("status") == "declared"
            and isinstance(prior_application.get("license_file"), str)
            and isinstance(prior_application.get("sha256"), str)
        ):
            prior_relative = _safe_distribution_path(
                prior_application["license_file"]
            )
            if len(prior_relative.parts) == 1:
                prior_hashes[prior_relative.as_posix()] = prior_application["sha256"]

    for candidate in release_dir.iterdir():
        if (
            not candidate.is_file()
            or candidate.name == NOTICES_NAME
            or candidate.name == current_name
            or not LICENSE_FILE_NAME_PATTERN.search(candidate.name)
        ):
            continue
        expected_hash = prior_hashes.get(candidate.name)
        if expected_hash and _sha256_file(candidate) == expected_hash:
            candidate.unlink()
            continue
        raise ValueError(
            "Release contains an untracked or modified possible stale application "
            f"license file: {candidate.name}. Use a clean release directory."
        )


def _cyclonedx_sbom(
    application_version: str,
    application_license: dict[str, Any],
    blockers: list[dict[str, str]],
    components: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    root: dict[str, Any] = {
        "bom-ref": f"pkg:pypi/automadin@{quote(application_version, safe='.+-')}",
        "name": "Automadin",
        "type": "application",
        "version": application_version,
    }
    if application_license["status"] == "declared":
        root["licenses"] = [
            {"expression": application_license["license_expression"]}
        ]
    root["properties"] = [
        {
            "name": "automadin:publication-readiness",
            "value": "blocked" if blockers else "ready",
        },
        {"name": "automadin:sbom-scope", "value": SBOM_SCOPE},
    ]

    sbom_components: list[dict[str, Any]] = []
    runtime_refs: list[str] = []
    for component in components:
        entry: dict[str, Any] = {
            "bom-ref": component["purl"],
            "licenses": [{"expression": component["license_expression"]}],
            "name": component["name"],
            "properties": [
                {"name": "automadin:release-scope", "value": component["scope"]},
                {
                    "name": "automadin:license-metadata-source",
                    "value": component["license_metadata"]["source"],
                },
                {
                    "name": "automadin:license-file-count",
                    "value": str(len(component["license_files"])),
                },
            ],
            "purl": component["purl"],
            "type": "library",
            "version": component["version"],
        }
        if component["source_urls"]:
            entry["externalReferences"] = [
                {
                    "comment": source["label"],
                    "type": "website",
                    "url": source["url"],
                }
                for source in component["source_urls"]
            ]
        sbom_components.append(entry)
        if component["scope"] == "runtime":
            runtime_refs.append(component["purl"])
    return {
        "bomFormat": "CycloneDX",
        "components": sbom_components,
        "dependencies": [
            {"dependsOn": sorted(runtime_refs), "ref": root["bom-ref"]}
        ],
        "metadata": {"component": root},
        "specVersion": "1.6",
        "version": 1,
    }


def _third_party_notices(
    application_version: str,
    application_license: dict[str, Any],
    blockers: list[dict[str, str]],
    components: Iterable[dict[str, Any]],
) -> str:
    lines = [
        "AUTOMADIN THIRD-PARTY NOTICES",
        f"Application version: {application_version}",
        "",
        "This deterministic index records declarations from the exact locked Python",
        "distributions used by the Windows release. Referenced evidence files are",
        "copied byte-for-byte from those installed distributions; their SHA-256",
        "hashes are recorded below. No license text is synthesized.",
        f"SBOM scope: {SBOM_SCOPE}",
        "Embedded native and installer components require separate coverage.",
        "",
        "Automadin's own licensing is separate from these third-party notices.",
        f"Application license status: {application_license['status']}",
    ]
    for blocker in blockers:
        lines.append(f"PUBLICATION BLOCKER [{blocker['code']}]: {blocker['message']}")
    lines.extend(["", "=" * 79, ""])
    for index, component in enumerate(components, start=1):
        lines.extend(
            [
                f"{index}. {component['name']} {component['version']}",
                f"Canonical name: {component['canonical_name']}",
                f"Release scope: {component['scope']}",
                f"Declared license: {component['license_expression']}",
                f"Metadata source: {component['license_metadata']['source']}",
                f"Package URL: {component['purl']}",
            ]
        )
        for source in component["source_urls"]:
            lines.append(f"Source ({source['label']}): {source['url']}")
        if component["license_files"]:
            lines.append("Packaged license/notice evidence:")
            for evidence in component["license_files"]:
                lines.append(
                    f"  - {evidence['path']} "
                    f"(SHA-256 {evidence['sha256']}, {evidence['size']} bytes)"
                )
        else:
            lines.append("Packaged license/notice evidence: none")
        if component.get("evidence_note"):
            lines.append(f"Evidence note: {component['evidence_note']}")
        lines.extend(["", "-" * 79, ""])
    return "\n".join(lines).rstrip() + "\n"


def generate_release_compliance(
    release_dir: Path,
    project_root: Path,
    lock_path: Path,
    policy_path: Path,
    *,
    source_dir: Path | None = None,
    source_policy_path: Path | None = None,
    native_policy_path: Path | None = None,
    inno_compiler: Path | None = None,
    distribution_provider: Callable[
        [str], importlib.metadata.Distribution
    ] = importlib.metadata.distribution,
) -> dict[str, Path]:
    """Generate release compliance artifacts without network access."""
    release_dir = release_dir.resolve()
    project_root = project_root.resolve()
    if not release_dir.is_dir():
        raise ValueError(f"Release directory does not exist: {release_dir}")
    pins = read_release_lock(lock_path.resolve(strict=True))
    policy = _read_policy(policy_path.resolve(strict=True))
    _validate_policy(policy, pins)
    application_version = read_project_version(project_root)
    application_license, blockers, application_evidence = _application_license(
        project_root, policy
    )
    blockers.extend(_policy_blockers(policy))
    source_offer: dict[str, Any] | None = None
    source_manifest: dict[str, Any] | None = None
    native_record: dict[str, Any] | None = None
    source_values = (source_dir, source_policy_path)
    if any(value is not None for value in source_values):
        if not all(value is not None for value in source_values):
            raise ValueError(
                "Source fulfillment requires source_dir and source_policy_path."
            )
        assert source_dir is not None and source_policy_path is not None
        source_manifest = verify_release_sources(
            project_root,
            source_dir,
            source_policy_path,
            lock_path,
        )
        fulfilled = source_manifest["fulfilled_blocker"]
        matches = [blocker for blocker in blockers if blocker["code"] == fulfilled]
        if len(matches) != 1:
            raise ValueError(
                f"Source fulfillment targets unknown or duplicate blocker {fulfilled!r}."
            )
        blockers = [blocker for blocker in blockers if blocker["code"] != fulfilled]
    native_values = (native_policy_path, inno_compiler)
    if any(value is not None for value in native_values):
        if not all(value is not None for value in native_values):
            raise ValueError(
                "Native inventory fulfillment requires native_policy_path and "
                "inno_compiler."
            )
        assert native_policy_path is not None and inno_compiler is not None
        native_inventory = verify_native_inventory(
            release_dir,
            project_root,
            lock_path,
            native_policy_path,
            inno_compiler=inno_compiler,
        )
        fulfilled = native_inventory["fulfilled_blocker"]
        matches = [blocker for blocker in blockers if blocker["code"] == fulfilled]
        if len(matches) != 1:
            raise ValueError(
                f"Native inventory targets unknown or duplicate blocker {fulfilled!r}."
            )
        blockers = [blocker for blocker in blockers if blocker["code"] != fulfilled]
        native_record = _native_inventory_record(release_dir, native_inventory)
    components = _collect_components(pins, policy, distribution_provider)

    _prepare_application_license_output(release_dir, application_license)
    output_files = [release_dir / SBOM_NAME, release_dir / NOTICES_NAME]
    license_directory = release_dir / LICENSE_DIRECTORY_NAME
    source_offer_directory = release_dir / SOURCE_OFFER_DIRECTORY_NAME
    for output in output_files:
        output.unlink(missing_ok=True)
    if license_directory.exists():
        if not license_directory.is_dir():
            raise ValueError(f"Compliance output is not a directory: {license_directory}")
        shutil.rmtree(license_directory)
    license_directory.mkdir()
    if source_offer_directory.exists():
        if not source_offer_directory.is_dir():
            raise ValueError(
                f"Source offer output is not a directory: {source_offer_directory}"
            )
        shutil.rmtree(source_offer_directory)

    if application_evidence is not None:
        relative_name, content = application_evidence
        application_target = release_dir / relative_name
        application_target.write_bytes(content)
    if source_dir is not None and source_manifest is not None:
        copy_source_offer(source_dir, release_dir)
        source_offer = _source_offer_record(release_dir, source_manifest)
    components = _write_evidence(release_dir, components)

    lock_hash = _sha256_canonical_text_file(lock_path)
    policy_hash = _sha256_canonical_text_file(policy_path)
    license_manifest = {
        "application": {
            "name": "Automadin",
            "version": application_version,
            **application_license,
        },
        "components": components,
        "corresponding_source": source_offer,
        "native_inventory": native_record,
        "inputs": {
            "license_policy": {
                "canonicalization": INPUT_CANONICALIZATION,
                "name": policy_path.name,
                "sha256": policy_hash,
            },
            "release_lock": {
                "canonicalization": INPUT_CANONICALIZATION,
                "name": lock_path.name,
                "sha256": lock_hash,
            },
        },
        "publication_blockers": blockers,
        "sbom_scope": SBOM_SCOPE,
        "schema_version": 2,
    }
    manifest_path = license_directory / LICENSE_MANIFEST_NAME
    manifest_path.write_bytes(_json_bytes(license_manifest))

    sbom_path = release_dir / SBOM_NAME
    sbom_path.write_bytes(
        _json_bytes(
            _cyclonedx_sbom(
                application_version,
                application_license,
                blockers,
                components,
            )
        )
    )
    notices_path = release_dir / NOTICES_NAME
    notices_path.write_text(
        _third_party_notices(
            application_version,
            application_license,
            blockers,
            components,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return {
        "license_manifest": manifest_path,
        "notices": notices_path,
        "sbom": sbom_path,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--source-policy", type=Path)
    parser.add_argument("--native-policy", type=Path)
    parser.add_argument("--inno-compiler", type=Path)
    args = parser.parse_args()
    try:
        outputs = generate_release_compliance(
            args.release_dir,
            args.project_root,
            args.lock,
            args.policy,
            source_dir=args.source_dir,
            source_policy_path=args.source_policy,
            native_policy_path=args.native_policy,
            inno_compiler=args.inno_compiler,
        )
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        "Release compliance generated: "
        + ", ".join(str(outputs[name]) for name in sorted(outputs))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
