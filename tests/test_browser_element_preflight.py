from __future__ import annotations

from automadin.models.device import DeviceType
from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.services.flow_preflight import PreflightMode, preflight_flow


def _codes(flow: AutomationFlow, target: DeviceType = DeviceType.BROWSER) -> set[str]:
    return {
        issue.code
        for issue in preflight_flow(
            flow,
            target=target,
            mode=PreflightMode.EXECUTION,
        ).errors
    }


def test_current_element_click_is_valid_only_inside_browser_collection_loop() -> None:
    click = FlowCommand(
        "click_element",
        params={"target": "current_element", "locator": ""},
    )
    nested = AutomationFlow(
        "Nested",
        commands=[
            FlowCommand(
                "for_each_browser_element",
                params={"locator": ".row", "container_locator": ""},
                children=[click],
            )
        ],
    )
    outside = AutomationFlow(
        "Outside",
        commands=[FlowCommand("click_element", params={"target": "current_element"})],
    )

    assert "CURRENT_BROWSER_ELEMENT_OUT_OF_SCOPE" not in _codes(nested)
    assert "CURRENT_BROWSER_ELEMENT_OUT_OF_SCOPE" in _codes(outside)


def test_locator_target_still_requires_a_locator() -> None:
    flow = AutomationFlow(
        "Missing locator",
        commands=[FlowCommand("click_element", params={"target": "locator"})],
    )

    assert "MISSING_PARAMETER" in _codes(flow)


def test_scroll_target_requires_only_the_fields_relevant_to_its_mode() -> None:
    page = AutomationFlow(
        "Page scroll",
        commands=[FlowCommand("scroll_browser", params={"target": "page"})],
    )
    missing_locator = AutomationFlow(
        "Missing locator",
        commands=[FlowCommand("scroll_browser", params={"target": "locator"})],
    )
    outside = AutomationFlow(
        "Outside",
        commands=[FlowCommand("scroll_browser", params={"target": "current_element"})],
    )

    assert "MISSING_PARAMETER" not in _codes(page)
    assert "MISSING_PARAMETER" in _codes(missing_locator)
    assert "CURRENT_BROWSER_ELEMENT_OUT_OF_SCOPE" in _codes(outside)


def test_current_element_extract_requires_a_browser_collection_scope() -> None:
    extract = FlowCommand(
        "extract_text",
        params={"target": "current_element", "child_selector": "p.description"},
    )
    nested = AutomationFlow(
        "Nested",
        commands=[
            FlowCommand(
                "for_each_browser_element",
                params={"locator": ".row"},
                children=[extract],
            )
        ],
    )
    outside = AutomationFlow("Outside", commands=[extract])

    assert "CURRENT_BROWSER_ELEMENT_OUT_OF_SCOPE" not in _codes(nested)
    assert "CURRENT_BROWSER_ELEMENT_OUT_OF_SCOPE" in _codes(outside)


def test_browser_collection_loop_is_rejected_for_non_browser_target() -> None:
    flow = AutomationFlow(
        "Wrong target",
        commands=[
            FlowCommand("for_each_browser_element", params={"locator": ".row"})
        ],
    )

    assert "UNSUPPORTED_TARGET" in _codes(flow, DeviceType.WINDOWS)
