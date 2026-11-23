from __future__ import annotations

from PySide6.QtWidgets import QApplication

from automadin.execution.error_report import ExecutionErrorReport
from automadin.ui.execution_error_details import ExecutionErrorBar


def _report() -> ExecutionErrorReport:
    try:
        raise RuntimeError(r"private C:\Users\Example\secret-token.txt")
    except RuntimeError as exc:
        return ExecutionErrorReport.from_exception(
            exc,
            reference="A1B2C3D4",
            command_id="click_element",
            step_id="12345678-abcd",
        )


def test_error_report_keeps_log_summary_safe_and_details_useful() -> None:
    report = _report()

    assert report.log_message() == (
        "[1234] click_element — failed: RuntimeError while running the command. "
        "(reference A1B2C3D4)"
    )
    assert "secret-token.txt" not in report.log_message()
    assert "secret-token.txt" in report.details
    assert "Command: click_element" in report.copy_text()
    assert "Step: 12345678-abcd" in report.copy_text()


def test_error_bar_shows_report_and_copies_details(
    application: QApplication,
) -> None:
    bar = ExecutionErrorBar()
    report = _report()
    try:
        assert bar.isHidden()

        bar.show_report(report)
        assert not bar.isHidden()
        assert bar._label.text() == "Error reference A1B2C3D4"

        bar._copy_details()
        assert QApplication.clipboard().text() == report.copy_text()

        bar.clear_report()
        assert bar.isHidden()
    finally:
        bar.deleteLater()
        application.processEvents()
