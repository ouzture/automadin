from __future__ import annotations

from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "quality.yml"
)


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_quality_workflow_has_a_bounded_frozen_windows_release_gate() -> None:
    workflow = _workflow_text()

    assert "windows-release:" in workflow
    assert "runs-on: windows-2025" in workflow
    assert "timeout-minutes: 60" in workflow
    assert 'python-version: "3.11.4"' in workflow
    assert "requirements-release-windows.txt" in workflow
    assert "tools/build_windows.ps1 -Python $python -RequireInstaller" in workflow
    assert "tools/smoke_windows_installer.py" in workflow
    assert "--built-production-installer" in workflow
    assert "--built-production-scratch" in workflow
    assert "tools/smoke_windows_release.py" in workflow


def test_quality_workflow_is_manual_while_the_repository_is_private() -> None:
    workflow = _workflow_text()

    assert "workflow_dispatch:" in workflow
    assert "\n  push:" not in workflow
    assert "\n  pull_request:" not in workflow


def test_quality_workflow_does_not_publish_unsigned_release_artifacts() -> None:
    workflow = _workflow_text()

    assert "permissions:\n  contents: read" in workflow
    assert "upload-artifact" not in workflow
