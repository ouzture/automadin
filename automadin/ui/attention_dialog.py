"""Shared interactive resolution for flow attention checkpoints."""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget

from automadin.execution.attention import AttentionRequest, AttentionResolution


def prompt_for_attention(
    parent: QWidget,
    request: AttentionRequest,
) -> AttentionResolution:
    """Show the resolutions permitted by one attention request."""
    dialog = QMessageBox(parent)
    dialog.setWindowTitle("Flow Needs Your Attention")
    dialog.setIcon(QMessageBox.Icon.Warning)
    dialog.setText(request.message)
    has_retry = AttentionResolution.RETRY in request.allowed_resolutions
    dialog.setInformativeText(
        "Retry checks the same condition again. Continue follows the command's "
        "normal path. Stop ends this flow run."
        if has_retry
        else "Continue resumes the flow after you complete this manual step. "
        "Stop ends this flow run."
    )
    retry = (
        dialog.addButton("Retry", QMessageBox.ButtonRole.AcceptRole)
        if has_retry
        else None
    )
    continue_button = (
        dialog.addButton("Continue", QMessageBox.ButtonRole.ActionRole)
        if AttentionResolution.CONTINUE in request.allowed_resolutions
        else None
    )
    dialog.addButton("Stop", QMessageBox.ButtonRole.RejectRole)
    dialog.exec()
    clicked = dialog.clickedButton()
    if clicked is retry and retry is not None:
        return AttentionResolution.RETRY
    if clicked is continue_button and continue_button is not None:
        return AttentionResolution.CONTINUE
    return AttentionResolution.STOP


__all__ = ["prompt_for_attention"]
