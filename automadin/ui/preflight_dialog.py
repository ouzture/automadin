"""Small shared presentation boundary for semantic flow preflight results."""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget

from automadin.services.flow_preflight import PreflightReport


_MAX_VISIBLE_ISSUES = 20


def _report_text(report: PreflightReport, *, introduction: str) -> str:
    counts: list[str] = []
    if report.errors:
        counts.append(f"{len(report.errors)} error(s)")
    if report.warnings:
        counts.append(f"{len(report.warnings)} warning(s)")
    if report.infos:
        counts.append(f"{len(report.infos)} note(s)")

    lines = [introduction, "", f"Preflight found {', '.join(counts)}.", ""]
    severity_order = {"error": 0, "warning": 1, "info": 2}
    ordered_issues = sorted(
        report.issues,
        key=lambda issue: severity_order[issue.severity.value],
    )
    for issue in ordered_issues[:_MAX_VISIBLE_ISSUES]:
        location = issue.location
        lines.append(
            f"{issue.severity.value.upper()} · {location.display_text}\n{issue.message}"
        )
        if issue.remediation:
            lines.append(f"Fix: {issue.remediation}")
        lines.append("")

    remaining = len(ordered_issues) - _MAX_VISIBLE_ISSUES
    if remaining > 0:
        lines.append(f"…and {remaining} more issue(s).")
    return "\n".join(lines).rstrip()


def confirm_document_preflight(
    parent: QWidget,
    report: PreflightReport,
    *,
    title: str = "Flow Preflight",
) -> bool:
    """Allow a semantically invalid flow to be saved explicitly as a draft."""
    if not report.errors and not report.warnings:
        return True

    text = _report_text(
        report,
        introduction=(
            "This flow can be kept as a draft, but it cannot run while the "
            "errors below remain."
            if report.errors
            else "Review these non-blocking flow warnings."
        ),
    )
    if not report.errors:
        QMessageBox.information(parent, title, text)
        return True

    reply = QMessageBox.warning(
        parent,
        title,
        text,
        QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
        QMessageBox.StandardButton.Cancel,
    )
    return reply == QMessageBox.StandardButton.Save


def confirm_import_preflight(
    parent: QWidget,
    report: PreflightReport,
    *,
    title: str = "Imported Flow Preflight",
) -> bool:
    """Let the user inspect an imperfect imported flow without persisting it."""
    if not report.errors and not report.warnings:
        return True
    reply = QMessageBox.warning(
        parent,
        title,
        _report_text(
            report,
            introduction=(
                "The imported flow has problems. You can open it as an unsaved "
                "draft and repair it before running."
            ),
        ),
        QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Cancel,
        QMessageBox.StandardButton.Cancel,
    )
    return reply == QMessageBox.StandardButton.Open


def confirm_execution_preflight(
    parent: QWidget,
    report: PreflightReport,
    *,
    title: str = "Flow Cannot Run",
) -> bool:
    """Block proven execution errors and make deferred warnings explicit."""
    if report.errors:
        QMessageBox.critical(
            parent,
            title,
            _report_text(
                report,
                introduction="Fix these flow problems before starting execution.",
            ),
        )
        return False
    if not report.warnings:
        return True

    reply = QMessageBox.warning(
        parent,
        "Flow Preflight Warnings",
        _report_text(
            report,
            introduction=(
                "Some values can only be checked while the flow runs. "
                "Continue with these warnings?"
            ),
        ),
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        QMessageBox.StandardButton.Cancel,
    )
    return reply == QMessageBox.StandardButton.Yes
