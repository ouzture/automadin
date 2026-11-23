"""Read release metadata from the project's canonical configuration."""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def read_project_version(project_root: Path = PROJECT_ROOT) -> str:
    """Return the non-empty PEP 621 project version from ``pyproject.toml``."""
    pyproject_path = project_root / "pyproject.toml"
    with pyproject_path.open("rb") as pyproject_file:
        metadata = tomllib.load(pyproject_file)

    version = metadata.get("project", {}).get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"Missing [project].version in {pyproject_path}")
    return version.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the canonical project version (the default operation).",
    )
    parser.parse_args()
    print(read_project_version())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
