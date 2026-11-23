from __future__ import annotations

import pytest

from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.services import flow_preflight


@pytest.fixture
def locator_command() -> str:
    command_id = "wait_for_selector"
    locator = next(
        param
        for param in flow_preflight._COMMAND_TYPES[command_id]["params"]
        if param["name"] == "locator"
    )
    assert locator["type"] == "locator"
    assert locator["aliases"] == ["selector"]
    return command_id


def _report(command_id: str, locator: object):
    flow = AutomationFlow(
        name="locator",
        commands=[
            FlowCommand(
                id=command_id,
                step_id="00000000-0000-4000-8000-000000000001",
                params={"locator": locator, "timeout": 1000},
            )
        ],
    )
    return flow_preflight.preflight_flow(flow)


@pytest.mark.parametrize(
    "locator",
    [
        "#submit",
        {"strategy": "test_id", "value": "submit"},
        {"strategy": "role", "role": "button", "name": "Submit"},
    ],
)
def test_preflight_accepts_legacy_and_structured_locators(
    locator_command: str,
    locator: object,
) -> None:
    assert not _report(locator_command, locator).errors


def test_preflight_rejects_invalid_locator_structure(locator_command: str) -> None:
    report = _report(locator_command, {"strategy": "role", "name": "Submit"})

    assert [issue.code for issue in report.errors] == ["INVALID_PARAMETER_TYPE"]


def test_preflight_checks_templates_inside_structured_locator(
    locator_command: str,
) -> None:
    dynamic = _report(
        locator_command,
        {"strategy": "role", "role": "button", "name": "{{button_name}}"},
    )
    malformed = _report(
        locator_command,
        {"strategy": "role", "role": "button", "name": "{{ button_name"},
    )

    assert [issue.code for issue in dynamic.warnings] == ["DYNAMIC_REFERENCE"]
    assert [issue.code for issue in malformed.errors] == ["MALFORMED_TEMPLATE"]
