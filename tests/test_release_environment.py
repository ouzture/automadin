from __future__ import annotations

import importlib.metadata
from pathlib import Path

import pytest

from tools.verify_release_environment import (
    read_release_lock,
    release_environment_errors,
)


def test_checked_in_release_lock_is_exact_and_complete() -> None:
    lock = Path(__file__).resolve().parents[1] / "requirements-release-windows.txt"
    pins = read_release_lock(lock)

    assert len(pins) == 17
    assert {
        "nuitka",
        "pyside6",
        "playwright",
        "opencv-python",
        "numpy",
        "pillow",
        "tzdata",
        "pywin32",
        "windows-cap",
    }.issubset(pins)


@pytest.mark.parametrize(
    "content",
    [
        "playwright>=1.58\n",
        "playwright\n",
        "playwright==1.58\nPlaywright==1.58\n",
        "# comments only\n",
    ],
)
def test_release_lock_rejects_non_exact_duplicate_or_empty_pins(
    tmp_path: Path,
    content: str,
) -> None:
    lock = tmp_path / "release.txt"
    lock.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError):
        read_release_lock(lock)


def test_release_environment_reports_platform_python_and_package_drift() -> None:
    pins = {"playwright": ("playwright", "1.58.0")}

    errors = release_environment_errors(
        pins,
        version_provider=lambda _name: "1.57.0",
        python_version=(3, 12),
        system="Linux",
        machine="aarch64",
    )

    assert any("Windows is required" in error for error in errors)
    assert any("Python 3.11 is required" in error for error in errors)
    assert any("Windows x64 is required" in error for error in errors)
    assert any("playwright must be 1.58.0" in error for error in errors)


def test_release_environment_reports_missing_package() -> None:
    pins = {"pywin32": ("pywin32", "311")}

    def missing(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    assert release_environment_errors(
        pins,
        version_provider=missing,
        python_version=(3, 11),
        system="Windows",
        machine="AMD64",
    ) == ["pywin32==311 is not installed."]
