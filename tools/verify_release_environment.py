"""Fail when the Windows release interpreter differs from the checked-in lock."""

from __future__ import annotations

import argparse
import importlib.metadata
import platform
import re
import sys
from collections.abc import Callable
from pathlib import Path

PIN_PATTERN = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[A-Za-z0-9][A-Za-z0-9.!+_-]*)$"
)


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).casefold()


def read_release_lock(path: Path) -> dict[str, tuple[str, str]]:
    """Read exact package pins as canonical-name -> (display-name, version)."""
    pins: dict[str, tuple[str, str]] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN_PATTERN.fullmatch(line)
        if match is None:
            raise ValueError(
                f"{path.name}:{line_number} must contain one exact name==version pin."
            )
        name = match.group("name")
        version = match.group("version")
        canonical = _canonical_name(name)
        if canonical in pins:
            raise ValueError(
                f"{path.name}:{line_number} duplicates package {pins[canonical][0]}."
            )
        pins[canonical] = (name, version)
    if not pins:
        raise ValueError(f"{path.name} does not contain any release package pins.")
    return pins


def release_environment_errors(
    pins: dict[str, tuple[str, str]],
    *,
    version_provider: Callable[[str], str] = importlib.metadata.version,
    python_version: tuple[int, int] | None = None,
    system: str | None = None,
    machine: str | None = None,
) -> list[str]:
    """Return deterministic reasons the active interpreter cannot build a release."""
    current_python = python_version or (sys.version_info.major, sys.version_info.minor)
    current_system = system or platform.system()
    current_machine = (machine or platform.machine()).casefold()
    errors: list[str] = []
    if current_system != "Windows":
        errors.append(f"Windows is required; current platform is {current_system}.")
    if current_python != (3, 11):
        errors.append(
            "Python 3.11 is required; current interpreter is "
            f"{current_python[0]}.{current_python[1]}."
        )
    if current_machine not in {"amd64", "x86_64"}:
        errors.append(
            f"Windows x64 is required; current machine is {machine or platform.machine()}."
        )

    for _canonical, (name, expected) in sorted(pins.items()):
        try:
            actual = version_provider(name)
        except importlib.metadata.PackageNotFoundError:
            errors.append(f"{name}=={expected} is not installed.")
            continue
        except Exception as exc:
            errors.append(f"Could not inspect {name}: {type(exc).__name__}.")
            continue
        if actual != expected:
            errors.append(f"{name} must be {expected}; found {actual}.")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("lock_file", type=Path)
    args = parser.parse_args()
    try:
        pins = read_release_lock(args.lock_file.resolve(strict=True))
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    errors = release_environment_errors(pins)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(
        "Release environment verified: "
        f"Python {sys.version_info.major}.{sys.version_info.minor}, "
        f"{len(pins)} exact package pins."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
