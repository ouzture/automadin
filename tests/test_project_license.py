from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GPL3_CANONICAL_SHA256 = (
    "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"
)


def test_project_declares_exact_gpl3_only_license() -> None:
    license_path = PROJECT_ROOT / "LICENSE"
    license_bytes = license_path.read_bytes()
    assert hashlib.sha256(license_bytes).hexdigest() == GPL3_CANONICAL_SHA256
    assert b"GNU GENERAL PUBLIC LICENSE\n                       Version 3" in license_bytes
    assert b"END OF TERMS AND CONDITIONS" in license_bytes

    metadata = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    assert metadata["license"] == "GPL-3.0-only"
    assert metadata["license-files"] == ["LICENSE", "NOTICE"]

    policy = json.loads(
        (PROJECT_ROOT / "release-license-policy.json").read_text(encoding="utf-8")
    )
    assert policy["application"] == {
        "license_expression": "GPL-3.0-only",
        "license_file": "LICENSE",
    }
    blocker_codes = {
        blocker["code"] for blocker in policy["publication_blockers"]
    }
    assert "application-license-missing" not in blocker_codes
    assert "qt-license-route-unresolved" not in blocker_codes
    assert "qt-gpl-source-distribution-incomplete" in blocker_codes


def test_public_documentation_states_gpl3_and_remaining_binary_obligations() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    distribution = (PROJECT_ROOT / "docs" / "distribution.md").read_text(
        encoding="utf-8"
    )

    assert "GPL-3.0-only" in readme
    assert "Corresponding Source" in readme
    assert "GPL-3.0-only" in distribution
    assert "Qt/PySide Corresponding Source" in distribution


def test_public_notice_identifies_copyright_and_canonical_repository() -> None:
    notice = (PROJECT_ROOT / "NOTICE").read_text(encoding="utf-8")

    assert "Copyright (C) 2026 ouzture" in notice
    assert "https://github.com/ouzture/automadin" in notice
    assert "GPL-3.0-only" not in notice
    assert "GNU General Public License, version 3 only" in notice
