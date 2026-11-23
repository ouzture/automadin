"""Validate generated release license evidence, notices, and CycloneDX SBOM."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
from collections.abc import Callable
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
    from tools.generate_release_compliance import (
        LICENSE_DIRECTORY_NAME,
        LICENSE_MANIFEST_NAME,
        INPUT_CANONICALIZATION,
        NOTICES_NAME,
        SBOM_NAME,
        SBOM_SCOPE,
        SOURCE_OFFER_DIRECTORY_NAME,
        VALID_SCOPE,
        _application_license,
        _canonical_name,
        _collect_components,
        _cyclonedx_sbom,
        _policy_blockers,
        _read_policy,
        _serialize_components,
        _sha256_canonical_text_file,
        _third_party_notices,
        _validate_policy,
    )
    from tools.project_metadata import read_project_version
    from tools.release_sources import (
        MANIFEST_NAME as SOURCE_MANIFEST_NAME,
        OFFER_NAME as SOURCE_OFFER_NAME,
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
    from generate_release_compliance import (
        LICENSE_DIRECTORY_NAME,
        LICENSE_MANIFEST_NAME,
        INPUT_CANONICALIZATION,
        NOTICES_NAME,
        SBOM_NAME,
        SBOM_SCOPE,
        SOURCE_OFFER_DIRECTORY_NAME,
        VALID_SCOPE,
        _application_license,
        _canonical_name,
        _collect_components,
        _cyclonedx_sbom,
        _policy_blockers,
        _read_policy,
        _serialize_components,
        _sha256_canonical_text_file,
        _third_party_notices,
        _validate_policy,
    )
    from project_metadata import read_project_version
    from release_sources import (
        MANIFEST_NAME as SOURCE_MANIFEST_NAME,
        OFFER_NAME as SOURCE_OFFER_NAME,
        verify_release_sources,
    )
    from verify_release_environment import read_release_lock


COMPLIANCE_ARTIFACTS = {
    "license_manifest": f"{LICENSE_DIRECTORY_NAME}/{LICENSE_MANIFEST_NAME}",
    "sbom": SBOM_NAME,
    "third_party_notices": NOTICES_NAME,
}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_artifact_path(release_dir: Path, relative_path: object) -> Path:
    if not isinstance(relative_path, str):
        raise ValueError("artifact path is not a string")
    pure_path = PurePosixPath(relative_path)
    if (
        not relative_path
        or pure_path.is_absolute()
        or any(part in {"", ".", ".."} for part in pure_path.parts)
        or any(":" in part or "\\" in part for part in pure_path.parts)
    ):
        raise ValueError(f"unsafe artifact path {relative_path!r}")
    release_root = release_dir.resolve()
    candidate = release_root.joinpath(*pure_path.parts).resolve()
    if not candidate.is_relative_to(release_root):
        raise ValueError(f"artifact escapes release directory: {relative_path}")
    return candidate


def _read_json(path: Path, label: str, errors: list[str]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"{label} is invalid: {exc}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{label} must contain a JSON object")
        return None
    return value


def _artifact_records(
    release_dir: Path,
    release_manifest: dict[str, Any],
    errors: list[str],
) -> dict[str, Path]:
    compliance = release_manifest.get("compliance")
    if not isinstance(compliance, dict):
        errors.append("release-manifest.json has no compliance artifact mapping")
        return {}
    if set(compliance) != set(COMPLIANCE_ARTIFACTS):
        errors.append(
            "release-manifest.json compliance mapping must contain exactly: "
            + ", ".join(sorted(COMPLIANCE_ARTIFACTS))
        )
    paths: dict[str, Path] = {}
    for key, expected_path in COMPLIANCE_ARTIFACTS.items():
        record = compliance.get(key)
        if not isinstance(record, dict):
            errors.append(f"release-manifest.json compliance.{key} is missing")
            continue
        if record.get("path") != expected_path:
            errors.append(
                f"release-manifest.json compliance.{key} must reference {expected_path}"
            )
            continue
        expected_hash = record.get("sha256")
        expected_size = record.get("size")
        if not isinstance(expected_hash, str) or not SHA256_PATTERN.fullmatch(
            expected_hash
        ):
            errors.append(
                f"release-manifest.json compliance.{key} has an invalid SHA-256"
            )
            continue
        if not isinstance(expected_size, int) or expected_size <= 0:
            errors.append(f"release-manifest.json compliance.{key} has an invalid size")
            continue
        try:
            path = _safe_artifact_path(release_dir, expected_path)
        except ValueError as exc:
            errors.append(f"release-manifest.json compliance.{key}: {exc}")
            continue
        if not path.is_file():
            errors.append(f"Compliance artifact is missing: {expected_path}")
            continue
        try:
            actual_size = path.stat().st_size
            actual_hash = _sha256_file(path)
        except OSError as exc:
            errors.append(f"Could not inspect compliance artifact {expected_path}: {exc}")
            continue
        if actual_size != expected_size:
            errors.append(
                f"Compliance artifact size mismatch for {expected_path}: "
                f"expected {expected_size}, found {actual_size}"
            )
        if actual_hash != expected_hash:
            errors.append(f"Compliance artifact SHA-256 mismatch: {expected_path}")
        paths[key] = path
    return paths


def _verify_corresponding_source_offer(
    release_dir: Path,
    license_manifest: dict[str, Any],
    blockers: list[dict[str, str]],
    errors: list[str],
) -> str | None:
    record = license_manifest.get("corresponding_source")
    offer_root = release_dir / SOURCE_OFFER_DIRECTORY_NAME
    if record is None:
        if offer_root.exists():
            errors.append("Release contains an untracked corresponding-source offer")
        return None
    if not isinstance(record, dict):
        errors.append("Corresponding-source record must be an object or null")
        return None
    fulfilled = record.get("fulfilled_blocker")
    files = record.get("files")
    if not isinstance(fulfilled, str) or not fulfilled or not isinstance(files, list):
        errors.append("Corresponding-source record is invalid")
        return None
    if any(blocker["code"] == fulfilled for blocker in blockers):
        errors.append("Fulfilled source blocker is still listed as unresolved")
    expected_paths = {
        f"{SOURCE_OFFER_DIRECTORY_NAME}/{SOURCE_MANIFEST_NAME}",
        f"{SOURCE_OFFER_DIRECTORY_NAME}/{SOURCE_OFFER_NAME}",
    }
    actual_paths: set[str] = set()
    for file_record in files:
        if not isinstance(file_record, dict):
            errors.append("Corresponding-source file record is invalid")
            continue
        relative = file_record.get("path")
        expected_hash = file_record.get("sha256")
        expected_size = file_record.get("size")
        if (
            not isinstance(relative, str)
            or relative in actual_paths
            or relative not in expected_paths
            or not isinstance(expected_hash, str)
            or not SHA256_PATTERN.fullmatch(expected_hash)
            or not isinstance(expected_size, int)
            or expected_size <= 0
        ):
            errors.append("Corresponding-source file metadata is invalid")
            continue
        actual_paths.add(relative)
        try:
            path = _safe_artifact_path(release_dir, relative)
        except ValueError as exc:
            errors.append(f"Corresponding-source file path is invalid: {exc}")
            continue
        if not path.is_file():
            errors.append(f"Corresponding-source offer file is missing: {relative}")
            continue
        if path.stat().st_size != expected_size:
            errors.append(f"Corresponding-source offer size mismatch: {relative}")
        if _sha256_file(path) != expected_hash:
            errors.append(f"Corresponding-source offer SHA-256 mismatch: {relative}")
    if actual_paths != expected_paths:
        errors.append("Corresponding-source record does not contain the exact offer files")
    manifest_path = offer_root / SOURCE_MANIFEST_NAME
    if manifest_path.is_file():
        source_manifest = _read_json(
            manifest_path, "Corresponding-source manifest", errors
        )
        if source_manifest is not None and source_manifest.get(
            "fulfilled_blocker"
        ) != fulfilled:
            errors.append("Corresponding-source fulfillment identity does not match")
    offer_path = offer_root / SOURCE_OFFER_NAME
    if offer_path.is_file():
        try:
            offer = offer_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"Corresponding-source offer is invalid: {exc}")
        else:
            if not offer.startswith("# Automadin Corresponding Source\n"):
                errors.append("Corresponding-source offer has an invalid heading")
    if offer_root.is_dir():
        found = {
            path.relative_to(release_dir).as_posix()
            for path in offer_root.rglob("*")
            if path.is_file()
        }
        if found != expected_paths:
            errors.append("Corresponding-source directory contains untracked files")
    return fulfilled


def _verify_native_inventory_record(
    release_dir: Path,
    license_manifest: dict[str, Any],
    blockers: list[dict[str, str]],
    errors: list[str],
) -> str | None:
    record = license_manifest.get("native_inventory")
    inventory_path = release_dir / NATIVE_INVENTORY_NAME
    evidence_root = release_dir / NATIVE_EVIDENCE_DIRECTORY
    if record is None:
        if inventory_path.exists() or evidence_root.exists():
            errors.append("Release contains an untracked native inventory")
        return None
    if not isinstance(record, dict):
        errors.append("Native inventory record must be an object or null")
        return None
    fulfilled = record.get("fulfilled_blocker")
    files = record.get("files")
    if not isinstance(fulfilled, str) or not fulfilled or not isinstance(files, list):
        errors.append("Native inventory record is invalid")
        return None
    if any(blocker["code"] == fulfilled for blocker in blockers):
        errors.append("Fulfilled native inventory blocker is still unresolved")
    expected_paths = {
        NATIVE_INVENTORY_NAME,
        f"{NATIVE_EVIDENCE_DIRECTORY}/{INNO_LICENSE_NAME}",
    }
    actual_paths: set[str] = set()
    for file_record in files:
        if not isinstance(file_record, dict):
            errors.append("Native inventory file record is invalid")
            continue
        relative = file_record.get("path")
        expected_hash = file_record.get("sha256")
        expected_size = file_record.get("size")
        if (
            not isinstance(relative, str)
            or relative in actual_paths
            or relative not in expected_paths
            or not isinstance(expected_hash, str)
            or not SHA256_PATTERN.fullmatch(expected_hash)
            or not isinstance(expected_size, int)
            or expected_size <= 0
        ):
            errors.append("Native inventory file metadata is invalid")
            continue
        actual_paths.add(relative)
        try:
            path = _safe_artifact_path(release_dir, relative)
        except ValueError as exc:
            errors.append(f"Native inventory path is invalid: {exc}")
            continue
        if not path.is_file():
            errors.append(f"Native inventory file is missing: {relative}")
            continue
        if path.stat().st_size != expected_size:
            errors.append(f"Native inventory size mismatch: {relative}")
        if _sha256_file(path) != expected_hash:
            errors.append(f"Native inventory SHA-256 mismatch: {relative}")
    if actual_paths != expected_paths:
        errors.append("Native inventory record does not contain the exact evidence files")
    if inventory_path.is_file():
        inventory = _read_json(inventory_path, "Native release inventory", errors)
        if inventory is not None and inventory.get("fulfilled_blocker") != fulfilled:
            errors.append("Native inventory fulfillment identity does not match")
    if evidence_root.is_dir():
        found = {
            path.relative_to(release_dir).as_posix()
            for path in evidence_root.rglob("*")
            if path.is_file()
        }
        if found != {f"{NATIVE_EVIDENCE_DIRECTORY}/{INNO_LICENSE_NAME}"}:
            errors.append("Native evidence directory contains untracked files")
    return fulfilled


def _license_components(
    release_dir: Path,
    license_manifest: dict[str, Any],
    application_version: str,
    errors: list[str],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    if license_manifest.get("schema_version") != 2:
        errors.append("Third-party license manifest has an unsupported schema version")
    if license_manifest.get("sbom_scope") != SBOM_SCOPE:
        errors.append("Third-party license manifest has an invalid SBOM scope")
    application = license_manifest.get("application")
    if not isinstance(application, dict):
        errors.append("Third-party license manifest has no application object")
    else:
        if application.get("name") != "Automadin":
            errors.append("Third-party license manifest has an invalid application name")
        if application.get("version") != application_version:
            errors.append(
                "Third-party license manifest application version does not match "
                "release-manifest.json"
            )

    blockers_value = license_manifest.get("publication_blockers")
    blockers: list[dict[str, str]] = []
    blocker_codes: set[str] = set()
    if not isinstance(blockers_value, list):
        errors.append("Third-party license manifest has no publication_blockers list")
    else:
        for blocker in blockers_value:
            if not isinstance(blocker, dict):
                errors.append("Third-party license manifest has an invalid blocker")
                continue
            code = blocker.get("code")
            message = blocker.get("message")
            if (
                not isinstance(code, str)
                or not code
                or not isinstance(message, str)
                or not message
            ):
                errors.append("Third-party license manifest has an invalid blocker")
                continue
            if code in blocker_codes:
                errors.append(f"Third-party license manifest duplicates blocker {code}")
                continue
            blocker_codes.add(code)
            blockers.append({"code": code, "message": message})

    if isinstance(application, dict):
        status = application.get("status")
        if status == "missing":
            if "application-license-missing" not in blocker_codes:
                errors.append(
                    "Missing application license is not recorded as a publication blocker"
                )
        elif status == "declared":
            expression = application.get("license_expression")
            relative_path = application.get("license_file")
            expected_hash = application.get("sha256")
            if (
                not isinstance(expression, str)
                or not expression
                or not isinstance(expected_hash, str)
                or not SHA256_PATTERN.fullmatch(expected_hash)
            ):
                errors.append("Declared application license metadata is invalid")
            else:
                try:
                    path = _safe_artifact_path(release_dir, relative_path)
                except ValueError as exc:
                    errors.append(f"Declared application license path is invalid: {exc}")
                else:
                    if not path.is_file():
                        errors.append("Declared application license file is missing")
                    elif _sha256_file(path) != expected_hash:
                        errors.append("Declared application license SHA-256 mismatch")
        else:
            errors.append("Third-party license manifest has an invalid application status")

    components_value = license_manifest.get("components")
    if not isinstance(components_value, list) or not components_value:
        errors.append("Third-party license manifest has no components")
        return {}, blockers
    components: dict[str, dict[str, Any]] = {}
    tracked_files: set[str] = {COMPLIANCE_ARTIFACTS["license_manifest"]}
    for component in components_value:
        if not isinstance(component, dict):
            errors.append("Third-party license manifest contains a non-object component")
            continue
        canonical = component.get("canonical_name")
        name = component.get("name")
        version = component.get("version")
        purl = component.get("purl")
        expression = component.get("license_expression")
        scope = component.get("scope")
        if (
            not isinstance(canonical, str)
            or not canonical
            or not isinstance(name, str)
            or not name
            or not isinstance(version, str)
            or not version
            or not isinstance(purl, str)
            or not purl.startswith("pkg:pypi/")
            or not isinstance(expression, str)
            or not expression
            or scope not in VALID_SCOPE
        ):
            errors.append("Third-party license manifest contains invalid component identity")
            continue
        if canonical in components:
            errors.append(f"Third-party license manifest duplicates {canonical}")
            continue
        license_metadata = component.get("license_metadata")
        if (
            not isinstance(license_metadata, dict)
            or not isinstance(license_metadata.get("source"), str)
            or not license_metadata["source"]
        ):
            errors.append(f"Third-party component {canonical} has no license metadata source")
        evidence_value = component.get("license_files")
        if not isinstance(evidence_value, list):
            errors.append(f"Third-party component {canonical} has no license_files list")
            evidence_value = []
        if not evidence_value and not str(component.get("evidence_note", "")).strip():
            errors.append(
                f"Third-party component {canonical} has neither evidence nor an exception note"
            )
        seen_component_paths: set[str] = set()
        for evidence in evidence_value:
            if not isinstance(evidence, dict):
                errors.append(f"Third-party component {canonical} has invalid evidence")
                continue
            relative_path = evidence.get("path")
            expected_hash = evidence.get("sha256")
            expected_size = evidence.get("size")
            source_path = evidence.get("source_distribution_path")
            expected_prefix = f"{LICENSE_DIRECTORY_NAME}/{canonical}/"
            if (
                not isinstance(relative_path, str)
                or not relative_path.startswith(expected_prefix)
                or relative_path in seen_component_paths
                or not isinstance(expected_hash, str)
                or not SHA256_PATTERN.fullmatch(expected_hash)
                or not isinstance(expected_size, int)
                or expected_size <= 0
                or not isinstance(source_path, str)
                or not source_path
            ):
                errors.append(f"Third-party component {canonical} has invalid evidence metadata")
                continue
            seen_component_paths.add(relative_path)
            if relative_path in tracked_files:
                errors.append(f"Third-party evidence path is duplicated: {relative_path}")
                continue
            tracked_files.add(relative_path)
            try:
                path = _safe_artifact_path(release_dir, relative_path)
            except ValueError as exc:
                errors.append(f"Third-party evidence path is invalid: {exc}")
                continue
            if not path.is_file():
                errors.append(f"Third-party evidence is missing: {relative_path}")
                continue
            if path.stat().st_size != expected_size:
                errors.append(f"Third-party evidence size mismatch: {relative_path}")
            if _sha256_file(path) != expected_hash:
                errors.append(f"Third-party evidence SHA-256 mismatch: {relative_path}")
        components[canonical] = component

    license_root = release_dir / LICENSE_DIRECTORY_NAME
    if license_root.is_dir():
        actual_files = {
            path.relative_to(release_dir).as_posix()
            for path in license_root.rglob("*")
            if path.is_file()
        }
        for unexpected in sorted(actual_files - tracked_files):
            errors.append(f"Untracked third-party license file: {unexpected}")
    return components, blockers


def _verify_sbom(
    sbom: dict[str, Any],
    application_version: str,
    components: dict[str, dict[str, Any]],
    blockers: list[dict[str, str]],
    errors: list[str],
) -> None:
    if (
        sbom.get("bomFormat") != "CycloneDX"
        or sbom.get("specVersion") != "1.6"
        or sbom.get("version") != 1
    ):
        errors.append("release-sbom.cdx.json is not CycloneDX 1.6 version 1")
    metadata = sbom.get("metadata")
    root = metadata.get("component") if isinstance(metadata, dict) else None
    if not isinstance(root, dict):
        errors.append("release-sbom.cdx.json has no root application component")
        return
    if root.get("name") != "Automadin" or root.get("version") != application_version:
        errors.append("release-sbom.cdx.json has invalid application identity")
    root_ref = root.get("bom-ref")
    if not isinstance(root_ref, str) or not root_ref:
        errors.append("release-sbom.cdx.json application has no bom-ref")

    expected_readiness = "blocked" if blockers else "ready"
    properties = root.get("properties")
    readiness = None
    sbom_scope = None
    if isinstance(properties, list):
        for prop in properties:
            if (
                isinstance(prop, dict)
                and prop.get("name") == "automadin:publication-readiness"
            ):
                readiness = prop.get("value")
            if (
                isinstance(prop, dict)
                and prop.get("name") == "automadin:sbom-scope"
            ):
                sbom_scope = prop.get("value")
    if readiness != expected_readiness:
        errors.append("release-sbom.cdx.json has incorrect publication readiness")
    if sbom_scope != SBOM_SCOPE:
        errors.append("release-sbom.cdx.json has an invalid SBOM scope")

    sbom_components = sbom.get("components")
    if not isinstance(sbom_components, list):
        errors.append("release-sbom.cdx.json has no component list")
        return
    by_purl: dict[str, dict[str, Any]] = {}
    for component in sbom_components:
        if not isinstance(component, dict) or not isinstance(component.get("purl"), str):
            errors.append("release-sbom.cdx.json contains an invalid component")
            continue
        purl = component["purl"]
        if purl in by_purl:
            errors.append(f"release-sbom.cdx.json duplicates component {purl}")
            continue
        by_purl[purl] = component
    expected_purls = {component["purl"] for component in components.values()}
    if set(by_purl) != expected_purls:
        errors.append(
            "release-sbom.cdx.json components do not match the third-party manifest"
        )
    for component in components.values():
        sbom_component = by_purl.get(component["purl"])
        if sbom_component is None:
            continue
        licenses = sbom_component.get("licenses")
        expected_licenses = [{"expression": component["license_expression"]}]
        if (
            sbom_component.get("name") != component["name"]
            or sbom_component.get("version") != component["version"]
            or sbom_component.get("bom-ref") != component["purl"]
            or licenses != expected_licenses
        ):
            errors.append(
                f"release-sbom.cdx.json metadata differs for {component['canonical_name']}"
            )

    dependencies = sbom.get("dependencies")
    expected_runtime = sorted(
        component["purl"]
        for component in components.values()
        if component["scope"] == "runtime"
    )
    if dependencies != [{"dependsOn": expected_runtime, "ref": root_ref}]:
        errors.append("release-sbom.cdx.json runtime dependency graph is invalid")


def _verify_trusted_sources(
    release_dir: Path,
    release_manifest: dict[str, Any],
    license_manifest: dict[str, Any],
    sbom: dict[str, Any] | None,
    notices: str,
    components: dict[str, dict[str, Any]],
    blockers: list[dict[str, str]],
    *,
    project_root: Path,
    lock_path: Path,
    policy_path: Path,
    source_dir: Path | None,
    source_policy_path: Path | None,
    native_policy_path: Path | None,
    inno_compiler: Path | None,
    distribution_provider: Callable[
        [str], importlib.metadata.Distribution
    ],
    errors: list[str],
) -> list[dict[str, str]]:
    """Bind generated claims to the checked-in lock, policy, and project metadata."""
    try:
        project_root = project_root.resolve(strict=True)
        lock_path = lock_path.resolve(strict=True)
        policy_path = policy_path.resolve(strict=True)
        pins = read_release_lock(lock_path)
        policy = _read_policy(policy_path)
        _validate_policy(policy, pins)
        application_version = read_project_version(project_root)
        expected_application, expected_blockers, _evidence = _application_license(
            project_root, policy
        )
        expected_blockers.extend(_policy_blockers(policy))
        source_record = license_manifest.get("corresponding_source")
        if source_record is not None:
            if source_dir is None or source_policy_path is None:
                raise ValueError(
                    "trusted corresponding-source verification requires the source "
                    "directory and source policy"
                )
            source_manifest = verify_release_sources(
                project_root,
                source_dir,
                source_policy_path.resolve(strict=True),
                lock_path,
            )
            fulfilled = source_manifest["fulfilled_blocker"]
            matches = [
                blocker for blocker in expected_blockers if blocker["code"] == fulfilled
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"corresponding source fulfills unknown blocker {fulfilled!r}"
                )
            expected_blockers = [
                blocker for blocker in expected_blockers if blocker["code"] != fulfilled
            ]
        native_record = license_manifest.get("native_inventory")
        if native_record is not None:
            if native_policy_path is None or inno_compiler is None:
                raise ValueError(
                    "trusted native inventory verification requires the native "
                    "policy and Inno compiler"
                )
            native_inventory = verify_native_inventory(
                release_dir,
                project_root,
                lock_path,
                native_policy_path.resolve(strict=True),
                inno_compiler=inno_compiler,
            )
            fulfilled = native_inventory["fulfilled_blocker"]
            matches = [
                blocker for blocker in expected_blockers if blocker["code"] == fulfilled
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"native inventory fulfills unknown blocker {fulfilled!r}"
                )
            expected_blockers = [
                blocker for blocker in expected_blockers if blocker["code"] != fulfilled
            ]
        expected_components = _serialize_components(
            _collect_components(pins, policy, distribution_provider)
        )
    except (OSError, ValueError) as exc:
        errors.append(f"Trusted compliance inputs are invalid: {exc}")
        return []

    inputs = license_manifest.get("inputs")
    expected_inputs = {
        "license_policy": {
            "canonicalization": INPUT_CANONICALIZATION,
            "name": policy_path.name,
            "sha256": _sha256_canonical_text_file(policy_path),
        },
        "release_lock": {
            "canonicalization": INPUT_CANONICALIZATION,
            "name": lock_path.name,
            "sha256": _sha256_canonical_text_file(lock_path),
        },
    }
    if inputs != expected_inputs:
        errors.append(
            "Third-party license manifest inputs do not match the trusted lock and policy"
        )

    expected_application_record = {
        "name": "Automadin",
        "version": application_version,
        **expected_application,
    }
    if license_manifest.get("application") != expected_application_record:
        errors.append(
            "Third-party license manifest application does not match trusted project licensing"
        )
    release_application = release_manifest.get("application")
    if (
        not isinstance(release_application, dict)
        or release_application.get("version") != application_version
    ):
        errors.append(
            "release-manifest.json application version does not match the trusted project"
        )

    if blockers != expected_blockers:
        errors.append(
            "Third-party license manifest publication blockers do not match trusted policy"
        )

    expected_canonicals = set(pins)
    if set(components) != expected_canonicals:
        missing = sorted(expected_canonicals - set(components))
        extra = sorted(set(components) - expected_canonicals)
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unexpected " + ", ".join(extra))
        errors.append(
            "Third-party license manifest components do not match the trusted release lock"
            + (f": {'; '.join(details)}" if details else "")
        )

    canonical_policy = {
        _canonical_name(str(name)): package_policy
        for name, package_policy in policy["packages"].items()
    }
    trusted_components: list[dict[str, Any]] = []
    for canonical, (locked_name, locked_version) in sorted(pins.items()):
        component = components.get(canonical)
        if component is None:
            continue
        package_policy = canonical_policy[canonical]
        expected_purl = (
            f"pkg:pypi/{quote(canonical, safe='.-')}@"
            f"{quote(locked_version, safe='.+-')}"
        )
        identity_valid = (
            component.get("canonical_name") == canonical
            and _canonical_name(str(component.get("name", ""))) == canonical
            and component.get("name") == locked_name
            and component.get("version") == locked_version
            and component.get("license_expression")
            == package_policy["license_expression"]
            and component.get("scope") == package_policy["scope"]
            and component.get("purl") == expected_purl
        )
        if not identity_valid:
            errors.append(
                f"Third-party component {canonical} does not match trusted lock/policy identity"
            )
        evidence = component.get("license_files")
        if package_policy["evidence_required"] and (
            not isinstance(evidence, list) or not evidence
        ):
            errors.append(
                f"Third-party component {canonical} lacks policy-required evidence"
            )
        expected_note = package_policy.get("evidence_note")
        if component.get("evidence_note") != expected_note:
            errors.append(
                f"Third-party component {canonical} evidence note differs from trusted policy"
            )
        trusted_components.append(component)

    if license_manifest.get("components") != expected_components:
        errors.append(
            "Third-party license manifest components differ from installed locked "
            "distribution metadata or evidence"
        )

    build = release_manifest.get("build")
    build_packages = build.get("packages") if isinstance(build, dict) else None
    if isinstance(build_packages, dict):
        for display_name, recorded_version in build_packages.items():
            canonical = _canonical_name(str(display_name))
            if canonical not in pins:
                continue
            expected_version = pins[canonical][1]
            if recorded_version != expected_version:
                errors.append(
                    "release-manifest.json build version differs from trusted lock for "
                    f"{display_name}: expected {expected_version}, found {recorded_version}"
                )

    if len(trusted_components) == len(pins):
        try:
            expected_notices = _third_party_notices(
                application_version,
                expected_application,
                expected_blockers,
                trusted_components,
            )
            if notices != expected_notices:
                errors.append(
                    "THIRD-PARTY-NOTICES.txt does not exactly match trusted compliance data"
                )
            if sbom is not None:
                expected_sbom = _cyclonedx_sbom(
                    application_version,
                    expected_application,
                    expected_blockers,
                    trusted_components,
                )
                if sbom != expected_sbom:
                    errors.append(
                        "release-sbom.cdx.json does not exactly match trusted compliance data"
                    )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"Trusted compliance rendering failed: {exc}")
    return expected_blockers


def verify_release_compliance(
    release_dir: Path,
    release_manifest: dict[str, Any],
    *,
    require_compliance_ready: bool = False,
    project_root: Path | None = None,
    lock_path: Path | None = None,
    policy_path: Path | None = None,
    source_dir: Path | None = None,
    source_policy_path: Path | None = None,
    native_policy_path: Path | None = None,
    inno_compiler: Path | None = None,
    distribution_provider: Callable[
        [str], importlib.metadata.Distribution
    ] = importlib.metadata.distribution,
) -> list[str]:
    """Return structural errors and, when supplied, trusted-source integrity errors."""
    errors: list[str] = []
    trusted_values = (project_root, lock_path, policy_path)
    has_trusted_inputs = all(value is not None for value in trusted_values)
    if any(value is not None for value in trusted_values) and not has_trusted_inputs:
        errors.append(
            "Trusted compliance verification requires project_root, lock_path, and policy_path"
        )
    if require_compliance_ready and not has_trusted_inputs:
        errors.append(
            "Compliance readiness requires trusted project, release lock, and license policy inputs"
        )
    paths = _artifact_records(release_dir, release_manifest, errors)
    if set(paths) != set(COMPLIANCE_ARTIFACTS):
        return errors
    application = release_manifest.get("application")
    application_version = (
        application.get("version") if isinstance(application, dict) else None
    )
    if not isinstance(application_version, str) or not application_version:
        return errors

    license_manifest = _read_json(
        paths["license_manifest"], "Third-party license manifest", errors
    )
    sbom = _read_json(paths["sbom"], "release-sbom.cdx.json", errors)
    try:
        notices = paths["third_party_notices"].read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"THIRD-PARTY-NOTICES.txt is invalid: {exc}")
        notices = ""
    if not notices.startswith("AUTOMADIN THIRD-PARTY NOTICES\n"):
        errors.append("THIRD-PARTY-NOTICES.txt has an invalid heading")
    if f"Application version: {application_version}\n" not in notices:
        errors.append("THIRD-PARTY-NOTICES.txt has an invalid application version")

    if license_manifest is None:
        return errors
    components, blockers = _license_components(
        release_dir, license_manifest, application_version, errors
    )
    fulfilled_source_blocker = _verify_corresponding_source_offer(
        release_dir,
        license_manifest,
        blockers,
        errors,
    )
    fulfilled_native_blocker = _verify_native_inventory_record(
        release_dir,
        license_manifest,
        blockers,
        errors,
    )
    for component in components.values():
        marker = f"{component['name']} {component['version']}\n"
        if marker not in notices:
            errors.append(
                f"THIRD-PARTY-NOTICES.txt omits {component['name']} "
                f"{component['version']}"
            )
    if sbom is not None:
        _verify_sbom(
            sbom,
            application_version,
            components,
            blockers,
            errors,
        )
    readiness_blockers = blockers
    if has_trusted_inputs:
        readiness_blockers = _verify_trusted_sources(
            release_dir,
            release_manifest,
            license_manifest,
            sbom,
            notices,
            components,
            blockers,
            project_root=project_root,
            lock_path=lock_path,
            policy_path=policy_path,
            source_dir=source_dir,
            source_policy_path=source_policy_path,
            native_policy_path=native_policy_path,
            inno_compiler=inno_compiler,
            distribution_provider=distribution_provider,
            errors=errors,
        )
    if (source_dir is None) != (source_policy_path is None):
        errors.append(
            "Trusted source fulfillment requires source_dir and source_policy_path"
        )
    elif (
        require_compliance_ready
        and fulfilled_source_blocker is not None
        and source_dir is None
    ):
        errors.append(
            "Compliance readiness requires trusted corresponding-source inputs"
        )
    if (native_policy_path is None) != (inno_compiler is None):
        errors.append(
            "Trusted native inventory requires native_policy_path and inno_compiler"
        )
    elif require_compliance_ready and fulfilled_native_blocker is not None:
        if native_policy_path is None or inno_compiler is None:
            errors.append(
                "Compliance readiness requires trusted native inventory inputs"
            )
    if require_compliance_ready and has_trusted_inputs:
        for blocker in readiness_blockers:
            errors.append(
                f"Publication blocker [{blocker['code']}]: {blocker['message']}"
            )
    return errors
