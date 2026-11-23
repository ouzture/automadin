from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from automadin.execution.build_executor import build_executor
from automadin.execution.executor import FlowTerminate
from automadin.execution.handlers.system_power import handle_hibernate_computer
from automadin.models.flow import FlowCommand
from automadin.services.flow_preflight import TargetKind, supported_targets
from automadin.subprocess_utils import HIDDEN_PROCESS_CREATION_FLAGS


def _context(logs: list[str] | None = None) -> SimpleNamespace:
    listener = None
    if logs is not None:
        listener = SimpleNamespace(on_log=logs.append)
    context = SimpleNamespace(listener=listener, stopped=False)
    context.stop = lambda: setattr(context, "stopped", True)
    return context


def test_hibernate_computer_requests_windows_hibernation_and_terminates_flow() -> None:
    completed = subprocess.CompletedProcess(
        ["shutdown.exe", "/h"], returncode=0, stdout="", stderr=""
    )
    logs: list[str] = []

    with (
        patch("automadin.execution.handlers.system_power.sys.platform", "win32"),
        patch(
            "automadin.execution.handlers.system_power.subprocess.run",
            return_value=completed,
        ) as run,
        pytest.raises(FlowTerminate),
    ):
        context = _context(logs)
        handle_hibernate_computer(FlowCommand("hibernate_computer"), context)

    run.assert_called_once_with(
        ["shutdown.exe", "/h"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
    )
    assert context.stopped is True
    assert logs == [
        "Windows accepted the hibernation request; ending automation run"
    ]


def test_hibernate_computer_reports_rejected_request() -> None:
    completed = subprocess.CompletedProcess(
        ["shutdown.exe", "/h"],
        returncode=1,
        stdout="",
        stderr="Hibernation is not enabled.",
    )

    with (
        patch("automadin.execution.handlers.system_power.sys.platform", "win32"),
        patch(
            "automadin.execution.handlers.system_power.subprocess.run",
            return_value=completed,
        ),
        pytest.raises(RuntimeError, match="Hibernation is not enabled"),
    ):
        handle_hibernate_computer(FlowCommand("hibernate_computer"), _context())


def test_hibernate_computer_rejects_non_windows_hosts() -> None:
    with (
        patch("automadin.execution.handlers.system_power.sys.platform", "linux"),
        patch("automadin.execution.handlers.system_power.subprocess.run") as run,
        pytest.raises(RuntimeError, match="requires Windows"),
    ):
        handle_hibernate_computer(FlowCommand("hibernate_computer"), _context())

    run.assert_not_called()


def test_hibernate_computer_is_registered_for_every_flow_target() -> None:
    assert "hibernate_computer" in build_executor().registered_handler_ids
    assert supported_targets("hibernate_computer") == frozenset(TargetKind)
