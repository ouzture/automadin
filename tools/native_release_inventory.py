"""Classify every native release binary and record the installer build tool."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

try:
    from tools.project_metadata import read_project_version
    from tools.verify_release_environment import read_release_lock
except ModuleNotFoundError as exc:
    if exc.name != "tools":
        raise
    from project_metadata import read_project_version
    from verify_release_environment import read_release_lock


INVENTORY_NAME = "native-release-inventory.json"
EVIDENCE_DIRECTORY = "native-license-evidence"
INNO_LICENSE_NAME = "Inno-Setup-LICENSE.txt"
NATIVE_SUFFIXES = {".dll", ".exe", ".pyd"}
INNO_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){1,3}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def read_native_policy(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read native policy {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Native policy must be an object with schema_version 1.")
    components = value.get("components")
    blocker = value.get("fulfilled_blocker")
    if not isinstance(components, list) or not isinstance(blocker, str) or not blocker:
        raise ValueError("Native policy has invalid components or blocker metadata.")
    seen_ids: set[str] = set()
    for component in components:
        if not isinstance(component, dict):
            raise ValueError("Native component policy must be an object.")
        identifier = component.get("id")
        expression = component.get("license_expression")
        patterns = component.get("patterns")
        source = component.get("version_source")
        if (
            not isinstance(identifier, str)
            or not identifier
            or identifier in seen_ids
            or not isinstance(expression, str)
            or not expression
            or not isinstance(patterns, list)
            or not patterns
            or not all(isinstance(pattern, str) and pattern for pattern in patterns)
            or source not in {"application", "file-hash", "package", "python"}
            or (source == "package" and not isinstance(component.get("package"), str))
        ):
            raise ValueError(f"Native component policy is invalid: {component!r}")
        seen_ids.add(identifier)
    return value


def _classify(relative: str, components: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [
        component
        for component in components
        if any(
            fnmatch.fnmatchcase(relative.casefold(), pattern.casefold())
            for pattern in component["patterns"]
        )
    ]
    if len(matches) != 1:
        if not matches:
            raise ValueError(f"Native binary is not classified: {relative}")
        raise ValueError(
            f"Native binary matches multiple components: {relative}: "
            + ", ".join(component["id"] for component in matches)
        )
    return matches[0]


def _component_version(
    component: dict[str, Any],
    *,
    application_version: str,
    python_version: str,
    pins: dict[str, tuple[str, str]],
) -> str:
    source = component["version_source"]
    if source == "application":
        return application_version
    if source == "python":
        return python_version
    if source == "file-hash":
        return "hash-identified"
    package = component["package"].replace("_", "-").casefold()
    if package not in pins:
        raise ValueError(
            f"Native component {component['id']} references unlocked package {package}."
        )
    return pins[package][1]


def generate_native_inventory(
    release_dir: Path,
    project_root: Path,
    lock_path: Path,
    policy_path: Path,
    *,
    python_version: str,
    inno_compiler: Path,
    inno_version: str,
    inno_license: Path,
) -> Path:
    release_dir = release_dir.resolve(strict=True)
    project_root = project_root.resolve(strict=True)
    lock_path = lock_path.resolve(strict=True)
    policy_path = policy_path.resolve(strict=True)
    inno_compiler = inno_compiler.resolve(strict=True)
    inno_license = inno_license.resolve(strict=True)
    if not INNO_VERSION_PATTERN.fullmatch(inno_version):
        raise ValueError(f"Inno Setup version is invalid: {inno_version!r}")
    if tuple(int(part) for part in inno_version.split(".")) < (6, 7):
        raise ValueError("Inno Setup 6.7 or newer is required.")
    if not python_version or any(character.isspace() for character in python_version):
        raise ValueError("Python version is invalid.")
    policy = read_native_policy(policy_path)
    pins = read_release_lock(lock_path)
    application_version = read_project_version(project_root)
    by_id: dict[str, dict[str, Any]] = {
        component["id"]: {
            "files": [],
            "id": component["id"],
            "license_expression": component["license_expression"],
            "package": component.get("package"),
            "version": _component_version(
                component,
                application_version=application_version,
                python_version=python_version,
                pins=pins,
            ),
        }
        for component in policy["components"]
    }
    binaries = sorted(
        (
            path
            for path in release_dir.rglob("*")
            if path.is_file() and path.suffix.casefold() in NATIVE_SUFFIXES
        ),
        key=lambda path: path.relative_to(release_dir).as_posix().casefold(),
    )
    if not binaries:
        raise ValueError("Release contains no native binaries to inventory.")
    for binary in binaries:
        relative = binary.relative_to(release_dir).as_posix()
        component = _classify(relative, policy["components"])
        by_id[component["id"]]["files"].append(
            {
                "path": relative,
                "sha256": _sha256(binary),
                "size": binary.stat().st_size,
            }
        )
    empty = sorted(identifier for identifier, record in by_id.items() if not record["files"])
    if empty:
        raise ValueError("Native policy components matched no files: " + ", ".join(empty))

    evidence_dir = release_dir / EVIDENCE_DIRECTORY
    if evidence_dir.exists():
        if not evidence_dir.is_dir():
            raise ValueError(f"Native evidence output is not a directory: {evidence_dir}")
        shutil.rmtree(evidence_dir)
    evidence_dir.mkdir()
    inno_license_target = evidence_dir / INNO_LICENSE_NAME
    shutil.copyfile(inno_license, inno_license_target)
    inventory = {
        "application_version": application_version,
        "build_tools": [
            {
                "id": "inno-setup",
                "license": {
                    "path": f"{EVIDENCE_DIRECTORY}/{INNO_LICENSE_NAME}",
                    "sha256": _sha256(inno_license_target),
                    "size": inno_license_target.stat().st_size,
                },
                "license_expression": "LicenseRef-Inno-Setup",
                "sha256": _sha256(inno_compiler),
                "version": inno_version,
            }
        ],
        "components": sorted(by_id.values(), key=lambda item: item["id"]),
        "fulfilled_blocker": policy["fulfilled_blocker"],
        "policy": {"name": policy_path.name, "sha256": hashlib.sha256(_json_bytes(policy)).hexdigest()},
        "python_version": python_version,
        "schema_version": 1,
    }
    destination = release_dir / INVENTORY_NAME
    destination.write_bytes(_json_bytes(inventory))
    verify_native_inventory(
        release_dir,
        project_root,
        lock_path,
        policy_path,
        inno_compiler=inno_compiler,
    )
    return destination


def verify_native_inventory(
    release_dir: Path,
    project_root: Path,
    lock_path: Path,
    policy_path: Path,
    *,
    inno_compiler: Path | None = None,
) -> dict[str, Any]:
    release_dir = release_dir.resolve(strict=True)
    project_root = project_root.resolve(strict=True)
    lock_path = lock_path.resolve(strict=True)
    policy_path = policy_path.resolve(strict=True)
    policy = read_native_policy(policy_path)
    try:
        inventory = json.loads((release_dir / INVENTORY_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Native release inventory is invalid: {exc}") from exc
    if (
        not isinstance(inventory, dict)
        or inventory.get("schema_version") != 1
        or inventory.get("application_version") != read_project_version(project_root)
        or inventory.get("fulfilled_blocker") != policy["fulfilled_blocker"]
        or inventory.get("policy")
        != {"name": policy_path.name, "sha256": hashlib.sha256(_json_bytes(policy)).hexdigest()}
    ):
        raise ValueError("Native release inventory identity is invalid.")
    components = inventory.get("components")
    if not isinstance(components, list):
        raise ValueError("Native release inventory has no components list.")
    policy_by_id = {component["id"]: component for component in policy["components"]}
    inventory_by_id = {
        component.get("id"): component
        for component in components
        if isinstance(component, dict) and isinstance(component.get("id"), str)
    }
    if set(inventory_by_id) != set(policy_by_id) or len(inventory_by_id) != len(components):
        raise ValueError("Native inventory components do not match policy.")
    tracked_paths: set[str] = set()
    for identifier, component in inventory_by_id.items():
        policy_component = policy_by_id[identifier]
        if (
            component.get("license_expression")
            != policy_component["license_expression"]
            or component.get("package") != policy_component.get("package")
            or not isinstance(component.get("version"), str)
            or not component["version"]
            or not isinstance(component.get("files"), list)
            or not component["files"]
        ):
            raise ValueError(f"Native inventory component is invalid: {identifier}")
        for record in component["files"]:
            if not isinstance(record, dict):
                raise ValueError(f"Native inventory file is invalid: {identifier}")
            relative = record.get("path")
            if not isinstance(relative, str) or relative in tracked_paths:
                raise ValueError("Native inventory contains an invalid or duplicate path.")
            path = release_dir.joinpath(*relative.split("/"))
            if (
                not path.is_file()
                or path.suffix.casefold() not in NATIVE_SUFFIXES
                or path.stat().st_size != record.get("size")
                or _sha256(path) != record.get("sha256")
                or _classify(relative, policy["components"])["id"] != identifier
            ):
                raise ValueError(f"Native inventory file failed verification: {relative}")
            tracked_paths.add(relative)
    actual_paths = {
        path.relative_to(release_dir).as_posix()
        for path in release_dir.rglob("*")
        if path.is_file() and path.suffix.casefold() in NATIVE_SUFFIXES
    }
    if tracked_paths != actual_paths:
        raise ValueError("Native inventory does not cover the exact native payload.")
    tools = inventory.get("build_tools")
    if not isinstance(tools, list) or len(tools) != 1 or tools[0].get("id") != "inno-setup":
        raise ValueError("Native inventory has invalid installer tool evidence.")
    tool = tools[0]
    if not INNO_VERSION_PATTERN.fullmatch(str(tool.get("version", ""))):
        raise ValueError("Native inventory has an invalid Inno Setup version.")
    if inno_compiler is not None and _sha256(inno_compiler.resolve(strict=True)) != tool.get(
        "sha256"
    ):
        raise ValueError("Inno Setup compiler does not match the native inventory.")
    license_record = tool.get("license")
    license_path = release_dir / EVIDENCE_DIRECTORY / INNO_LICENSE_NAME
    if (
        not isinstance(license_record, dict)
        or license_record.get("path")
        != f"{EVIDENCE_DIRECTORY}/{INNO_LICENSE_NAME}"
        or not license_path.is_file()
        or license_path.stat().st_size != license_record.get("size")
        or _sha256(license_path) != license_record.get("sha256")
    ):
        raise ValueError("Inno Setup license evidence is invalid.")
    return inventory


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--python-version", required=True)
    parser.add_argument("--inno-compiler", required=True, type=Path)
    parser.add_argument("--inno-version", required=True)
    parser.add_argument("--inno-license", required=True, type=Path)
    args = parser.parse_args()
    try:
        destination = generate_native_inventory(
            args.release_dir,
            args.project_root,
            args.lock,
            args.policy,
            python_version=args.python_version,
            inno_compiler=args.inno_compiler,
            inno_version=args.inno_version,
            inno_license=args.inno_license,
        )
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Native release inventory written: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
