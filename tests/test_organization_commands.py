from __future__ import annotations

from automadin.execution.build_executor import build_executor
from automadin.execution.context import FlowContext
from automadin.execution.handlers.registry import get_default_params, load_command_types
from automadin.models.device import DeviceSession
from automadin.models.flow import AutomationFlow, FlowCommand


class _LogListener:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def on_log(self, message: str) -> None:
        self.messages.append(message)

    def on_step_start(self, _command_id: str, _step_id: str) -> None:
        pass

    def on_step_end(self, _command_id: str, _step_id: str) -> None:
        pass

    def on_error(self, _message: str) -> None:
        pass

    def on_complete(self, _message: str) -> None:
        pass


def test_organization_command_metadata() -> None:
    command_types = load_command_types()

    section = command_types["section"]
    group = command_types["group"]
    assert section["category"] == "ORGANIZATION"
    assert section["canHaveChildren"] is False
    assert get_default_params(section) == {"text": ""}
    assert group["canHaveChildren"] is True
    assert group["collapseChildren"] is True
    assert get_default_params(group) == {"text": ""}


def _executor_with_probe(calls: list[str]):
    executor = build_executor()
    executor.register_handler("probe", lambda cmd, _ctx: calls.append(cmd.params["name"]))
    # The probe is test-only and deliberately absent from command metadata.
    executor._enforce_preflight = False
    return executor


def test_section_is_noop_and_group_executes_children_in_order() -> None:
    calls: list[str] = []
    executor = _executor_with_probe(calls)
    flow = AutomationFlow(
        "Organized",
        [
            FlowCommand("probe", params={"name": "before"}),
            FlowCommand("section", params={"text": "Middle"}),
            FlowCommand(
                "group",
                params={"text": "Grouped work"},
                children=[
                    FlowCommand("probe", params={"name": "first child"}),
                    FlowCommand("probe", params={"name": "second child"}),
                ],
            ),
            FlowCommand("probe", params={"name": "after"}),
        ],
    )

    executor.execute(flow, FlowContext(DeviceSession()))

    assert calls == ["before", "first child", "second child", "after"]


def test_disabled_group_skips_its_children() -> None:
    calls: list[str] = []
    executor = _executor_with_probe(calls)

    executor.execute(
        AutomationFlow(
            "Disabled group",
            [
                FlowCommand(
                    "group",
                    enabled=False,
                    children=[FlowCommand("probe", params={"name": "child"})],
                )
            ],
        ),
        FlowContext(DeviceSession()),
    )

    assert calls == []


def test_section_and_group_titles_are_written_to_execution_log() -> None:
    listener = _LogListener()
    build_executor().execute(
        AutomationFlow(
            "Titles",
            [
                FlowCommand("section", params={"text": "Sign in"}),
                FlowCommand("group", params={"text": "Account checks"}),
            ],
        ),
        FlowContext(DeviceSession(), listener),
    )

    assert "Section — Sign in" in listener.messages
    assert "Group — Account checks (0 steps)" in listener.messages
