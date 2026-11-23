"""Prepare and verify the source set published beside a Windows binary release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

try:
    from tools.project_metadata import read_project_version
    from tools.verify_release_environment import read_release_lock
except ModuleNotFoundError as exc:
    if exc.name != "tools":
        raise
    from project_metadata import read_project_version
    from verify_release_environment import read_release_lock


MANIFEST_NAME = "corresponding-source-manifest.json"
OFFER_NAME = "SOURCE-OFFER.md"
POLICY_NAME = "release-source-policy.json"
SOURCE_SET_SCHEMA = 1
SHA256_LENGTH = 64
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
REQUIRED_AUTOMADIN_SOURCE_FILES = {
    "LICENSE",
    "pyproject.toml",
    "pysidedeploy.spec",
    "requirements-release-windows.txt",
    "release-license-policy.json",
    "release-native-policy.json",
    "release-source-policy.json",
    "packaging/Automadin.iss",
    "tools/build_windows.ps1",
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(DOWNLOAD_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def read_source_policy(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read source policy {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Source policy must be an object with schema_version 1.")
    blocker = value.get("fulfilled_blocker")
    archives = value.get("archives")
    if not isinstance(blocker, str) or not blocker or not isinstance(archives, list):
        raise ValueError("Source policy has invalid blocker or archives metadata.")
    seen_ids: set[str] = set()
    seen_files: set[str] = set()
    for archive in archives:
        if not isinstance(archive, dict):
            raise ValueError("Source policy archive must be an object.")
        archive_id = archive.get("id")
        filename = archive.get("filename")
        digest = archive.get("sha256")
        size = archive.get("size")
        url = archive.get("url")
        version = archive.get("version")
        package = archive.get("package")
        license_expression = archive.get("license_expression")
        if (
            not isinstance(archive_id, str)
            or not archive_id
            or archive_id in seen_ids
            or not isinstance(filename, str)
            or PurePosixPath(filename).name != filename
            or filename in seen_files
            or not isinstance(digest, str)
            or len(digest) != SHA256_LENGTH
            or any(character not in "0123456789abcdef" for character in digest)
            or not isinstance(size, int)
            or size <= 0
            or not isinstance(url, str)
            or not url.startswith("https://download.qt.io/")
            or not isinstance(version, str)
            or not version
            or not isinstance(package, str)
            or not package
            or not isinstance(license_expression, str)
            or not license_expression
        ):
            raise ValueError(f"Source policy archive is invalid: {archive!r}")
        seen_ids.add(archive_id)
        seen_files.add(filename)
    return value


def _run_git(project_root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(project_root), *arguments],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Git command failed ({' '.join(arguments)}): {detail}")
    return result.stdout


def _tracked_files(project_root: Path) -> tuple[str, ...]:
    raw = _run_git(project_root, "ls-files", "-z")
    try:
        files = tuple(
            sorted(
                item.decode("utf-8")
                for item in raw.split(b"\0")
                if item
            )
        )
    except UnicodeDecodeError as exc:
        raise ValueError("Git contains a tracked path that is not UTF-8.") from exc
    if not files or not REQUIRED_AUTOMADIN_SOURCE_FILES.issubset(files):
        missing = sorted(REQUIRED_AUTOMADIN_SOURCE_FILES - set(files))
        raise ValueError(
            "Tracked release source is incomplete"
            + (f": missing {', '.join(missing)}" if missing else ".")
        )
    return files


def _assert_clean_tracked_tree(project_root: Path) -> None:
    status = _run_git(project_root, "status", "--porcelain", "--untracked-files=no")
    if status:
        raise ValueError(
            "Corresponding source must be built from a commit with no tracked changes."
        )


def _create_automadin_archive(
    project_root: Path,
    destination: Path,
    version: str,
) -> tuple[str, ...]:
    tracked = _tracked_files(project_root)
    _assert_clean_tracked_tree(project_root)
    prefix = f"Automadin-{version}/"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        _run_git(
            project_root,
            "archive",
            "--format=zip",
            f"--prefix={prefix}",
            f"--output={temporary}",
            "HEAD",
        )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    _verify_automadin_archive(destination, tracked, version)
    return tracked


def _verify_automadin_archive(
    archive: Path,
    tracked_files: tuple[str, ...],
    version: str,
) -> None:
    prefix = f"Automadin-{version}/"
    expected = {prefix + relative for relative in tracked_files}
    try:
        with zipfile.ZipFile(archive) as source_zip:
            members = source_zip.infolist()
            actual_files: set[str] = set()
            for member in members:
                path = PurePosixPath(member.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in member.filename
                    or not member.filename.startswith(prefix)
                ):
                    raise ValueError(
                        f"Automadin source archive contains unsafe path {member.filename!r}."
                    )
                unix_mode = member.external_attr >> 16
                if unix_mode & 0o170000 == 0o120000:
                    raise ValueError(
                        f"Automadin source archive contains symlink {member.filename!r}."
                    )
                if not member.is_dir():
                    actual_files.add(member.filename)
            if actual_files != expected:
                missing = sorted(expected - actual_files)
                extra = sorted(actual_files - expected)
                detail = []
                if missing:
                    detail.append("missing " + ", ".join(missing[:5]))
                if extra:
                    detail.append("unexpected " + ", ".join(extra[:5]))
                raise ValueError(
                    "Automadin source archive differs from tracked HEAD"
                    + (f": {'; '.join(detail)}" if detail else ".")
                )
            pyproject = source_zip.read(prefix + "pyproject.toml").decode("utf-8")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile, KeyError) as exc:
        raise ValueError(f"Automadin source archive is invalid: {exc}") from exc
    marker = f'version = "{version}"'
    if marker not in pyproject:
        raise ValueError("Automadin source archive version does not match the release.")


def _download(
    url: str,
    destination: Path,
    opener: Callable[[str], BinaryIO],
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=destination.name + ".",
        suffix=".download",
        dir=destination.parent,
    )
    os.close(file_descriptor)
    temporary = Path(temporary_name)
    try:
        with opener(url) as response, temporary.open("wb") as target:
            shutil.copyfileobj(response, target, length=DOWNLOAD_CHUNK_SIZE)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_locked_versions(
    policy: dict[str, Any],
    lock_path: Path,
) -> None:
    pins = read_release_lock(lock_path)
    for archive in policy["archives"]:
        package = archive["package"].replace("_", "-").casefold()
        if package not in pins:
            raise ValueError(f"Source policy package is not release-locked: {package}")
        locked_version = pins[package][1]
        if archive["version"] != locked_version:
            raise ValueError(
                f"Source policy {archive['id']} version {archive['version']} does not "
                f"match locked {archive['package']} {locked_version}."
            )


def _artifact_record(
    *,
    artifact_id: str,
    filename: str,
    license_expression: str,
    path: Path,
    provenance_url: str | None,
    version: str,
) -> dict[str, Any]:
    return {
        "filename": filename,
        "id": artifact_id,
        "license_expression": license_expression,
        "provenance_url": provenance_url,
        "sha256": _sha256_file(path),
        "size": path.stat().st_size,
        "version": version,
    }


def _offer_text(version: str, artifacts: list[dict[str, Any]]) -> str:
    lines = [
        "# Automadin Corresponding Source",
        "",
        f"This notice applies to Automadin {version}.",
        "",
        "The complete, hash-verified source archives listed below must be offered",
        "for download at no charge from the same release location as every Automadin",
        "binary, portable archive, or installer. Upstream URLs record provenance only;",
        "they are not a substitute for publishing and retaining these exact files.",
        "",
        "The Automadin archive contains the application source plus the scripts and",
        "locked inputs used to build and install it. The Qt and Qt for Python archives",
        "are the exact source releases corresponding to the bundled 6.10.2 binaries.",
        "",
        "## Required files",
        "",
    ]
    for artifact in artifacts:
        lines.extend(
            [
                f"- `{artifact['filename']}`",
                f"  - SHA-256: `{artifact['sha256']}`",
                f"  - Size: {artifact['size']} bytes",
                f"  - License: `{artifact['license_expression']}`",
            ]
        )
    lines.extend(
        [
            "",
            "Build and installation instructions are in `docs/distribution.md` inside",
            "the Automadin source archive. Recipients may replace the dynamically loaded",
            "Qt/PySide libraries with compatible modified builds; Automadin applies no",
            "signature or DRM restriction preventing this.",
            "",
        ]
    )
    return "\n".join(lines)


def prepare_release_sources(
    project_root: Path,
    output_dir: Path,
    policy_path: Path,
    lock_path: Path,
    *,
    download: bool = False,
    opener: Callable[[str], BinaryIO] = urllib.request.urlopen,
) -> dict[str, Path]:
    project_root = project_root.resolve(strict=True)
    output_dir = output_dir.resolve()
    policy_path = policy_path.resolve(strict=True)
    lock_path = lock_path.resolve(strict=True)
    policy = read_source_policy(policy_path)
    _validate_locked_versions(policy, lock_path)
    version = read_project_version(project_root)
    output_dir.mkdir(parents=True, exist_ok=True)

    app_filename = f"Automadin-Source-{version}.zip"
    app_archive = output_dir / app_filename
    tracked = _create_automadin_archive(project_root, app_archive, version)
    artifacts = [
        _artifact_record(
            artifact_id="automadin",
            filename=app_filename,
            license_expression="GPL-3.0-only",
            path=app_archive,
            provenance_url=None,
            version=version,
        )
    ]

    for archive in policy["archives"]:
        destination = output_dir / archive["filename"]
        valid = (
            destination.is_file()
            and destination.stat().st_size == archive["size"]
            and _sha256_file(destination) == archive["sha256"]
        )
        if not valid and download:
            destination.unlink(missing_ok=True)
            _download(archive["url"], destination, opener)
            valid = (
                destination.stat().st_size == archive["size"]
                and _sha256_file(destination) == archive["sha256"]
            )
        if not valid:
            raise ValueError(
                f"Required corresponding source is missing or invalid: {destination}. "
                "Use --download or provide the exact hash-pinned archive."
            )
        artifacts.append(
            _artifact_record(
                artifact_id=archive["id"],
                filename=archive["filename"],
                license_expression=archive["license_expression"],
                path=destination,
                provenance_url=archive["url"],
                version=archive["version"],
            )
        )

    artifacts.sort(key=lambda item: item["id"])
    manifest = {
        "application_version": version,
        "artifacts": artifacts,
        "fulfilled_blocker": policy["fulfilled_blocker"],
        "policy": {
            "name": policy_path.name,
            "sha256": hashlib.sha256(_json_bytes(policy)).hexdigest(),
        },
        "schema_version": SOURCE_SET_SCHEMA,
        "tracked_application_files": len(tracked),
    }
    manifest_path = output_dir / MANIFEST_NAME
    offer_path = output_dir / OFFER_NAME
    manifest_path.write_bytes(_json_bytes(manifest))
    offer_path.write_text(_offer_text(version, artifacts), encoding="utf-8", newline="\n")
    verify_release_sources(project_root, output_dir, policy_path, lock_path)
    return {"manifest": manifest_path, "offer": offer_path}


def verify_release_sources(
    project_root: Path,
    source_dir: Path,
    policy_path: Path,
    lock_path: Path,
) -> dict[str, Any]:
    project_root = project_root.resolve(strict=True)
    source_dir = source_dir.resolve(strict=True)
    policy_path = policy_path.resolve(strict=True)
    lock_path = lock_path.resolve(strict=True)
    policy = read_source_policy(policy_path)
    _validate_locked_versions(policy, lock_path)
    version = read_project_version(project_root)
    try:
        manifest = json.loads((source_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Corresponding source manifest is invalid: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != SOURCE_SET_SCHEMA
        or manifest.get("application_version") != version
        or manifest.get("fulfilled_blocker") != policy["fulfilled_blocker"]
        or manifest.get("policy")
        != {"name": policy_path.name, "sha256": hashlib.sha256(_json_bytes(policy)).hexdigest()}
    ):
        raise ValueError("Corresponding source manifest identity is invalid.")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("Corresponding source manifest has no artifacts list.")
    expected_ids = {"automadin", *(archive["id"] for archive in policy["archives"])}
    by_id = {
        artifact.get("id"): artifact
        for artifact in artifacts
        if isinstance(artifact, dict) and isinstance(artifact.get("id"), str)
    }
    if set(by_id) != expected_ids or len(by_id) != len(artifacts):
        raise ValueError("Corresponding source manifest artifact identities are invalid.")
    tracked = _tracked_files(project_root)
    app = by_id["automadin"]
    app_filename = f"Automadin-Source-{version}.zip"
    if app.get("filename") != app_filename or app.get("version") != version:
        raise ValueError("Automadin source artifact identity is invalid.")
    _verify_automadin_archive(source_dir / app_filename, tracked, version)
    expected_records: dict[str, dict[str, Any]] = {
        "automadin": _artifact_record(
            artifact_id="automadin",
            filename=app_filename,
            license_expression="GPL-3.0-only",
            path=source_dir / app_filename,
            provenance_url=None,
            version=version,
        )
    }
    for archive in policy["archives"]:
        path = source_dir / archive["filename"]
        if not path.is_file():
            raise ValueError(f"Corresponding source archive is missing: {path.name}")
        expected_records[archive["id"]] = _artifact_record(
            artifact_id=archive["id"],
            filename=archive["filename"],
            license_expression=archive["license_expression"],
            path=path,
            provenance_url=archive["url"],
            version=archive["version"],
        )
        if (
            expected_records[archive["id"]]["size"] != archive["size"]
            or expected_records[archive["id"]]["sha256"] != archive["sha256"]
        ):
            raise ValueError(f"Corresponding source archive failed policy hash: {path.name}")
    if artifacts != sorted(expected_records.values(), key=lambda item: item["id"]):
        raise ValueError("Corresponding source manifest records do not match the archives.")
    offer = source_dir / OFFER_NAME
    if offer.read_text(encoding="utf-8") != _offer_text(version, artifacts):
        raise ValueError("Corresponding source offer does not match the verified source set.")
    return manifest


def copy_source_offer(source_dir: Path, release_dir: Path) -> dict[str, Path]:
    source_dir = source_dir.resolve(strict=True)
    release_dir = release_dir.resolve(strict=True)
    target = release_dir / "corresponding-source"
    if target.exists():
        if not target.is_dir():
            raise ValueError(f"Source offer output is not a directory: {target}")
        shutil.rmtree(target)
    target.mkdir()
    outputs: dict[str, Path] = {}
    for name in (MANIFEST_NAME, OFFER_NAME):
        destination = target / name
        shutil.copyfile(source_dir / name, destination)
        outputs[name] = destination
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    outputs = prepare_release_sources(
        args.project_root,
        args.output_dir,
        args.policy,
        args.lock,
        download=args.download,
    )
    print(f"Corresponding source ready: {outputs['manifest'].parent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
