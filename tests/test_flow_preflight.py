from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

import pytest

from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.services.flow_preflight import (
    PreflightMode,
    PreflightSeverity,
    TargetKind,
    preflight_flow,
    supported_targets,
)


class FakeDatabase:
    def __init__(
        self,
        *,
        images: Iterable[str] = (),
        visual_rules: Iterable[str] = (),
        flows: Iterable[AutomationFlow] = (),
    ) -> None:
        self._images = tuple(images)
        self._visual_rules = tuple(visual_rules)
        self._flows = {flow.name: flow for flow in flows}

    def list_images(self) -> list[dict[str, object]]:
        return [
            {"name": name, "width": 1, "height": 1, "created_at": ""}
            for name in self._images
        ]

    def list_visual_rules(self) -> list[str]:
        return list(self._visual_rules)

    def load_flow(self, name: str) -> AutomationFlow | None:
        return self._flows.get(name)


class FailingResourceDatabase(FakeDatabase):
    def __init__(self, resource: str) -> None:
        super().__init__()
        self.resource = resource
        self.flow_load_calls = 0

    def list_images(self) -> list[dict[str, object]]:
        if self.resource == "images":
            raise RuntimeError("image lookup unavailable")
        return super().list_images()

    def list_visual_rules(self) -> list[str]:
        if self.resource == "visual_rules":
            raise RuntimeError("visual-rule lookup unavailable")
        return super().list_visual_rules()

    def load_flow(self, name: str) -> AutomationFlow | None:
        self.flow_load_calls += 1
        if self.resource == "flows":
            if self.flow_load_calls == 1:
                raise RuntimeError("flow lookup unavailable")
            return None
        return super().load_flow(name)


def command(
    command_id: str,
    *,
    step: int = 1,
    params: dict[str, object] | None = None,
    children: list[FlowCommand] | None = None,
    else_children: list[FlowCommand] | None = None,
    enabled: bool = True,
) -> FlowCommand:
    return FlowCommand(
        id=command_id,
        step_id=f"00000000-0000-4000-8000-{step:012d}",
        params={} if params is None else params,
        children=[] if children is None else children,
        else_children=[] if else_children is None else else_children,
        enabled=enabled,
    )


def flow(name: str, *commands: FlowCommand) -> AutomationFlow:
    return AutomationFlow(name=name, commands=list(commands))


def issues_with(report, code: str):
    return [issue for issue in report.issues if issue.code == code]


def test_dynamic_target_requires_only_its_active_detector_source() -> None:
    missing_images = preflight_flow(
        flow(
            "Dynamic",
            command(
                "click_dynamic_target",
                params={"target_source": "images"},
            ),
        ),
        db=FakeDatabase(),
    )
    assert issues_with(missing_images, "EMPTY_IMAGE_LIST")

    valid_images = preflight_flow(
        flow(
            "Dynamic",
            command(
                "click_dynamic_target",
                params={"target_source": "images", "images": ["pose.png"]},
            ),
        ),
        db=FakeDatabase(images=("pose.png",)),
    )
    assert not issues_with(valid_images, "EMPTY_IMAGE_LIST")
    assert not issues_with(valid_images, "MISSING_PARAMETER")

    missing_rule = preflight_flow(
        flow(
            "Dynamic",
            command(
                "click_dynamic_target",
                params={"target_source": "visual_rule"},
            ),
        ),
        db=FakeDatabase(),
    )
    assert issues_with(missing_rule, "MISSING_PARAMETER")

    valid_rule = preflight_flow(
        flow(
            "Dynamic",
            command(
                "click_dynamic_target",
                params={
                    "target_source": "visual_rule",
                    "target_rule": "Chest colours",
                },
            ),
        ),
        db=FakeDatabase(visual_rules=("Chest colours",)),
    )
    assert not issues_with(valid_rule, "MISSING_PARAMETER")
    assert not issues_with(valid_rule, "EMPTY_IMAGE_LIST")


def assert_issue(
    report,
    code: str,
    severity: PreflightSeverity,
    path: str,
    *,
    parameter: str = "",
) -> None:
    matches = [
        issue
        for issue in report.issues
        if issue.code == code
        and issue.severity is severity
        and issue.location.display_path == path
        and issue.location.parameter == parameter
    ]
    assert matches, [
        (
            issue.code,
            issue.severity,
            issue.location.display_path,
            issue.location.parameter,
        )
        for issue in report.issues
    ]


def test_valid_flow_has_no_issues_and_can_execute() -> None:
    report = preflight_flow(
        flow(
            "valid",
            command("wait", params={"ms": 25}),
            command("terminate_flow", step=2),
        ),
        db=FakeDatabase(),
        target=TargetKind.ANDROID,
        mode=PreflightMode.EXECUTION,
    )

    assert report.issues == ()
    assert report.errors == ()
    assert report.warnings == ()
    assert report.infos == ()
    assert report.can_execute
    assert report.visited_flows == ("valid",)


def test_unknown_command_is_an_error_at_its_location() -> None:
    report = preflight_flow(flow("root", command("not_registered")))

    assert_issue(
        report,
        "UNKNOWN_COMMAND",
        PreflightSeverity.ERROR,
        "commands[0]",
    )
    issue = issues_with(report, "UNKNOWN_COMMAND")[0]
    assert issue.location.flow_name == "root"
    assert issue.location.step_id == "00000000-0000-4000-8000-000000000001"
    assert issue.location.command_id == "not_registered"
    assert not report.can_execute


def test_invalid_and_duplicate_step_ids_include_nested_children_and_else_paths() -> None:
    duplicate = "00000000-0000-4000-8000-000000000099"
    parent = command(
        "if_elapsed",
        step=1,
        children=[command("wait", params={"ms": 1})],
        else_children=[command("wait", step=3, params={"ms": 1})],
    )
    parent.children[0].step_id = duplicate
    parent.else_children[0].step_id = duplicate
    parent.step_id = "not-a-uuid"

    report = preflight_flow(flow("root", parent))

    assert_issue(
        report,
        "INVALID_STEP_ID",
        PreflightSeverity.ERROR,
        "commands[0]",
    )
    duplicate_issues = issues_with(report, "DUPLICATE_STEP_ID")
    assert duplicate_issues
    assert {
        issue.location.display_path for issue in duplicate_issues
    } & {
        "commands[0].children[0]",
        "commands[0].else_children[0]",
    }


def test_invalid_command_field_param_types_enum_range_and_missing_required() -> None:
    bad_enabled = command("wait", params={"ms": 1})
    bad_enabled.enabled = "yes"  # type: ignore[assignment]
    report = preflight_flow(
        flow(
            "root",
            bad_enabled,
            command("wait", step=2, params={"ms": "soon"}),
            command(
                "click_position",
                step=3,
                params={
                    "coordinate_unit": "yards",
                    "x": 1.0,
                    "y": 2.0,
                    "click": "single",
                },
            ),
            command(
                "click_image",
                step=4,
                params={
                    "images": ["known"],
                    "threshold": 0.9,
                    "xmargin": -1_000_000,
                },
            ),
            command("navigate_to", step=5),
        ),
        db=FakeDatabase(images=("known",)),
    )

    assert_issue(
        report,
        "INVALID_COMMAND_FIELD",
        PreflightSeverity.ERROR,
        "commands[0]",
        parameter="enabled",
    )
    assert_issue(
        report,
        "INVALID_PARAMETER_TYPE",
        PreflightSeverity.ERROR,
        "commands[1]",
        parameter="ms",
    )
    assert_issue(
        report,
        "INVALID_PARAMETER_VALUE",
        PreflightSeverity.ERROR,
        "commands[2]",
        parameter="coordinate_unit",
    )
    assert_issue(
        report,
        "INVALID_PARAMETER_VALUE",
        PreflightSeverity.ERROR,
        "commands[3]",
        parameter="xmargin",
    )
    assert_issue(
        report,
        "MISSING_PARAMETER",
        PreflightSeverity.ERROR,
        "commands[4]",
        parameter="url",
    )


def test_illegal_children_and_else_are_reported_separately() -> None:
    report = preflight_flow(
        flow(
            "root",
            command(
                "wait",
                params={"ms": 1},
                children=[command("terminate_flow", step=2)],
                else_children=[command("terminate_flow", step=3)],
            ),
            command(
                "for",
                step=4,
                params={"count": 1},
                else_children=[command("terminate_flow", step=5)],
            ),
        )
    )

    assert_issue(
        report,
        "ILLEGAL_CHILDREN",
        PreflightSeverity.ERROR,
        "commands[0]",
    )
    assert_issue(
        report,
        "ILLEGAL_ELSE",
        PreflightSeverity.ERROR,
        "commands[0]",
    )
    assert_issue(
        report,
        "ILLEGAL_ELSE",
        PreflightSeverity.ERROR,
        "commands[1]",
    )


def test_disabled_broken_command_does_not_block_execution() -> None:
    broken = command(
        "not_registered",
        params={"selector": "{{ broken"},
        enabled=False,
    )

    report = preflight_flow(
        flow("root", broken, command("wait", step=2, params={"ms": 1})),
        target=TargetKind.ANDROID,
        mode=PreflightMode.EXECUTION,
    )

    assert not report.errors
    assert report.can_execute


@pytest.mark.parametrize(
    ("selector", "code", "severity"),
    [
        ("", "EMPTY_SELECTOR", PreflightSeverity.ERROR),
        ("{{selector}}", "DYNAMIC_REFERENCE", PreflightSeverity.WARNING),
        ("{{ selector", "MALFORMED_TEMPLATE", PreflightSeverity.ERROR),
    ],
)
def test_selector_validation(
    selector: str,
    code: str,
    severity: PreflightSeverity,
) -> None:
    report = preflight_flow(
        flow(
            "root",
            command(
                "wait_for_selector",
                params={"selector": selector, "timeout": 1000},
            ),
        )
    )

    assert_issue(
        report,
        code,
        severity,
        "commands[0]",
        parameter="selector",
    )


def test_evaluate_js_accepts_javascript_block_and_object_braces() -> None:
    report = preflight_flow(
        flow(
            "root",
            command(
                "evaluate_js",
                params={
                    "script": "() => { const result = {ok: true}; return result; }"
                },
            ),
        )
    )

    assert not issues_with(report, "MALFORMED_TEMPLATE")
    assert not report.errors


@pytest.mark.parametrize(
    "url",
    ["https://example.com", "https://localhost:8443/path", "example.com/path"],
)
def test_navigate_accepts_urls_with_a_hostname(url: str) -> None:
    report = preflight_flow(
        flow("root", command("navigate_to", params={"url": url}))
    )

    assert not issues_with(report, "INVALID_URL")


@pytest.mark.parametrize(
    ("command_id", "url"),
    [
        ("navigate_to", "http://"),
        ("navigate_to", "file:///tmp/no-host"),
        ("open_new_tab", "example.com"),
    ],
)
def test_nonempty_urls_require_a_supported_scheme_and_hostname(
    command_id: str,
    url: str,
) -> None:
    report = preflight_flow(
        flow("root", command(command_id, params={"url": url}))
    )

    assert_issue(
        report,
        "INVALID_URL",
        PreflightSeverity.ERROR,
        "commands[0]",
        parameter="url",
    )


def test_open_new_tab_allows_an_empty_optional_url() -> None:
    report = preflight_flow(
        flow("root", command("open_new_tab", params={"url": ""}))
    )

    assert not issues_with(report, "INVALID_URL")
    assert not issues_with(report, "MISSING_PARAMETER")


@pytest.mark.parametrize(
    ("images", "expected_code", "severity"),
    [
        ([], "EMPTY_IMAGE_LIST", PreflightSeverity.ERROR),
        (["missing"], "MISSING_IMAGE", PreflightSeverity.ERROR),
        (["known", "missing"], "MISSING_IMAGE", PreflightSeverity.WARNING),
        (["{{image_name}}"], "DYNAMIC_REFERENCE", PreflightSeverity.WARNING),
    ],
)
def test_image_list_references(
    images: list[str],
    expected_code: str,
    severity: PreflightSeverity,
) -> None:
    report = preflight_flow(
        flow(
            "root",
            command(
                "click_image",
                params={"images": images, "threshold": 0.9},
            ),
        ),
        db=FakeDatabase(images=("known",)),
    )

    assert_issue(
        report,
        expected_code,
        severity,
        "commands[0]",
        parameter="images",
    )


def test_mixed_literal_and_dynamic_images_make_a_missing_literal_nonblocking() -> None:
    report = preflight_flow(
        flow(
            "root",
            command(
                "click_image",
                params={
                    "images": ["missing", "{{fallback_image}}"],
                    "threshold": 0.9,
                },
            ),
        ),
        db=FakeDatabase(),
        mode=PreflightMode.EXECUTION,
    )

    assert_issue(
        report,
        "MISSING_IMAGE",
        PreflightSeverity.WARNING,
        "commands[0]",
        parameter="images",
    )
    assert not report.errors


@pytest.mark.parametrize(
    ("resource", "command_id", "params", "parameter", "message_fragment"),
    [
        (
            "images",
            "click_image",
            {"images": ["literal"], "threshold": 0.9},
            "images",
            "Could not verify images",
        ),
        (
            "visual_rules",
            "click_best_target",
            {"target_rule": "literal"},
            "target_rule",
            "Could not verify visual rules",
        ),
        (
            "flows",
            "run_flow",
            {"flow_name": "literal"},
            "flow_name",
            "Could not load flow",
        ),
    ],
)
def test_resource_lookup_failures_are_mode_sensitive(
    resource: str,
    command_id: str,
    params: dict[str, object],
    parameter: str,
    message_fragment: str,
) -> None:
    document_report = preflight_flow(
        flow("root", command(command_id, params=params)),
        db=FailingResourceDatabase(resource),
        mode=PreflightMode.DOCUMENT,
    )
    execution_report = preflight_flow(
        flow("root", command(command_id, params=params)),
        db=FailingResourceDatabase(resource),
        mode=PreflightMode.EXECUTION,
    )

    assert_issue(
        document_report,
        {
            "images": "MISSING_IMAGE",
            "visual_rules": "MISSING_VISUAL_RULE",
            "flows": "MISSING_FLOW",
        }[resource],
        PreflightSeverity.WARNING,
        "commands[0]",
        parameter=parameter,
    )
    assert message_fragment in document_report.issues[0].message
    assert document_report.can_execute
    assert_issue(
        execution_report,
        {
            "images": "MISSING_IMAGE",
            "visual_rules": "MISSING_VISUAL_RULE",
            "flows": "MISSING_FLOW",
        }[resource],
        PreflightSeverity.ERROR,
        "commands[0]",
        parameter=parameter,
    )
    assert not execution_report.can_execute


@pytest.mark.parametrize(
    ("resource", "command_id", "params"),
    [
        (
            "images",
            "click_image",
            {"images": ["literal"], "threshold": 0.9},
        ),
        (
            "visual_rules",
            "click_best_target",
            {"target_rule": "literal"},
        ),
    ],
)
def test_disabled_resource_lookup_failures_are_info(
    resource: str,
    command_id: str,
    params: dict[str, object],
) -> None:
    report = preflight_flow(
        flow("root", command(command_id, params=params, enabled=False)),
        db=FailingResourceDatabase(resource),
        mode=PreflightMode.EXECUTION,
    )

    lookup_issues = [
        issue for issue in report.issues if "lookup unavailable" in issue.message
    ]
    assert lookup_issues
    assert all(
        issue.severity is PreflightSeverity.INFO for issue in lookup_issues
    )
    assert report.can_execute


def test_subflow_lookup_failure_is_cached_and_never_becomes_hard_missing() -> None:
    db = FailingResourceDatabase("flows")
    report = preflight_flow(
        flow(
            "root",
            command("run_flow", params={"flow_name": "first"}),
            command("run_flow", step=2, params={"flow_name": "second"}),
        ),
        db=db,
        mode=PreflightMode.DOCUMENT,
    )

    missing_flow_issues = issues_with(report, "MISSING_FLOW")
    assert len(missing_flow_issues) == 2
    assert all(
        issue.severity is PreflightSeverity.WARNING
        and "does not exist" not in issue.message
        for issue in missing_flow_issues
    )
    assert db.flow_load_calls == 1


def test_legacy_image_name_is_validated_as_an_image_reference() -> None:
    existing = preflight_flow(
        flow(
            "root",
            command(
                "click_image",
                params={"image_name": "known", "threshold": 0.9},
            ),
        ),
        db=FakeDatabase(images=("known",)),
    )
    missing = preflight_flow(
        flow(
            "root",
            command(
                "click_image",
                params={"image_name": "missing", "threshold": 0.9},
            ),
        ),
        db=FakeDatabase(images=("known",)),
    )

    assert not issues_with(existing, "MISSING_IMAGE")
    assert_issue(
        missing,
        "MISSING_IMAGE",
        PreflightSeverity.ERROR,
        "commands[0]",
        parameter="image_name",
    )


def test_legacy_key_aliases_use_runtime_precedence_without_blocking() -> None:
    both = preflight_flow(
        flow(
            "root",
            command(
                "press_keycode",
                params={"keys": ["ENTER"], "keycode": 66},
            ),
        )
    )
    fallback = preflight_flow(
        flow(
            "root",
            command(
                "press_keycode",
                params={"keys": [], "keycode": 66},
            ),
        )
    )

    assert not both.errors
    assert_issue(
        both,
        "INVALID_PARAMETER_VALUE",
        PreflightSeverity.INFO,
        "commands[0]",
        parameter="keys",
    )
    assert not fallback.errors
    assert not issues_with(fallback, "MISSING_PARAMETER")


def test_missing_required_and_optional_visual_rule_references_are_errors() -> None:
    report = preflight_flow(
        flow(
            "root",
            command(
                "click_best_target",
                params={
                    "target_rule": "missing-required",
                    "bonus_rule": "missing-optional",
                },
            ),
        ),
        db=FakeDatabase(visual_rules=("known",)),
    )

    assert_issue(
        report,
        "MISSING_VISUAL_RULE",
        PreflightSeverity.ERROR,
        "commands[0]",
        parameter="target_rule",
    )
    assert_issue(
        report,
        "MISSING_VISUAL_RULE",
        PreflightSeverity.ERROR,
        "commands[0]",
        parameter="bonus_rule",
    )


def test_known_visual_rules_do_not_raise_reference_issues() -> None:
    report = preflight_flow(
        flow(
            "root",
            command(
                "click_best_target",
                params={"target_rule": "target", "bonus_rule": "bonus"},
            ),
        ),
        db=FakeDatabase(visual_rules=("target", "bonus")),
    )

    assert not issues_with(report, "MISSING_VISUAL_RULE")


def test_screenshot_path_is_ignored_when_saving_is_disabled(tmp_path: Path) -> None:
    report = preflight_flow(
        flow(
            "root",
            command(
                "screenshot",
                params={
                    "save_to_disk": False,
                    "save_path": str(tmp_path / "does-not-exist"),
                },
            ),
        )
    )

    assert not issues_with(report, "INVALID_OUTPUT_PATH")
    assert not issues_with(report, "UNWRITABLE_OUTPUT_PATH")


def test_screenshot_accepts_an_existing_writable_directory(tmp_path: Path) -> None:
    report = preflight_flow(
        flow(
            "root",
            command(
                "screenshot",
                params={"save_to_disk": True, "save_path": str(tmp_path)},
            ),
        )
    )

    assert not issues_with(report, "INVALID_OUTPUT_PATH")
    assert not issues_with(report, "UNWRITABLE_OUTPUT_PATH")


@pytest.mark.parametrize("save_path", ["", "missing"])
def test_screenshot_rejects_empty_or_missing_output_directories(
    tmp_path: Path,
    save_path: str,
) -> None:
    value = save_path if not save_path else str(tmp_path / save_path)
    report = preflight_flow(
        flow(
            "root",
            command(
                "screenshot",
                params={"save_to_disk": True, "save_path": value},
            ),
        )
    )

    assert_issue(
        report,
        "INVALID_OUTPUT_PATH",
        PreflightSeverity.ERROR,
        "commands[0]",
        parameter="save_path",
    )


def test_screenshot_rejects_an_unwritable_output_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    real_access = os.access
    monkeypatch.setattr(
        os,
        "access",
        lambda path, mode: False if Path(path) == output else real_access(path, mode),
    )

    report = preflight_flow(
        flow(
            "root",
            command(
                "screenshot",
                params={"save_to_disk": True, "save_path": str(output)},
            ),
        )
    )

    assert_issue(
        report,
        "UNWRITABLE_OUTPUT_PATH",
        PreflightSeverity.WARNING,
        "commands[0]",
        parameter="save_path",
    )

    execution_report = preflight_flow(
        flow(
            "root",
            command(
                "screenshot",
                params={"save_to_disk": True, "save_path": str(output)},
            ),
        ),
        mode=PreflightMode.EXECUTION,
    )
    assert_issue(
        execution_report,
        "UNWRITABLE_OUTPUT_PATH",
        PreflightSeverity.ERROR,
        "commands[0]",
        parameter="save_path",
    )


def test_supported_target_matrix() -> None:
    assert set(supported_targets("navigate_to")) == {TargetKind.BROWSER}
    assert set(supported_targets("win_focus")) == {TargetKind.WINDOWS}
    assert TargetKind.WINDOWS not in supported_targets("swipe")
    assert set(supported_targets("screenshot")) == set(TargetKind)


@pytest.mark.parametrize(
    ("target", "command_id", "params"),
    [
        (TargetKind.ANDROID, "navigate_to", {"url": "https://example.com"}),
        (TargetKind.BROWSER, "win_focus", {}),
        (
            TargetKind.WINDOWS,
            "swipe",
            {
                "coordinate_unit": "percent",
                "start_x": 0.1,
                "start_y": 0.1,
                "end_x": 0.9,
                "end_y": 0.9,
            },
        ),
    ],
)
def test_commands_reject_unsupported_execution_targets(
    target: TargetKind,
    command_id: str,
    params: dict[str, object],
) -> None:
    report = preflight_flow(
        flow("root", command(command_id, params=params)),
        target=target,
        mode=PreflightMode.EXECUTION,
    )

    assert_issue(
        report,
        "UNSUPPORTED_TARGET",
        PreflightSeverity.ERROR,
        "commands[0]",
    )


def test_android_clear_text_field_does_not_require_browser_selector() -> None:
    for params in ({}, {"selector": ""}, {"selector": 42}):
        report = preflight_flow(
            flow("root", command("clear_text_field", params=params)),
            target=TargetKind.ANDROID,
            mode=PreflightMode.EXECUTION,
        )

        assert not report.errors
        assert not issues_with(report, "MISSING_PARAMETER")
        assert not issues_with(report, "EMPTY_SELECTOR")


def test_browser_clear_text_field_requires_selector() -> None:
    report = preflight_flow(
        flow("root", command("clear_text_field")),
        target=TargetKind.BROWSER,
        mode=PreflightMode.EXECUTION,
    )

    assert_issue(
        report,
        "MISSING_PARAMETER",
        PreflightSeverity.ERROR,
        "commands[0]",
        parameter="selector",
    )


def test_target_neutral_clear_text_field_warns_about_browser_selector() -> None:
    report = preflight_flow(flow("root", command("clear_text_field")))

    assert not report.errors
    assert_issue(
        report,
        "MISSING_PARAMETER",
        PreflightSeverity.WARNING,
        "commands[0]",
        parameter="selector",
    )


@pytest.mark.parametrize("target", list(TargetKind))
def test_screenshot_supports_every_execution_target(target: TargetKind) -> None:
    report = preflight_flow(
        flow("root", command("screenshot")),
        target=target,
        mode=PreflightMode.EXECUTION,
    )

    assert not issues_with(report, "UNSUPPORTED_TARGET")


@pytest.mark.parametrize(
    ("flow_name", "code", "severity"),
    [
        ("missing", "MISSING_FLOW", PreflightSeverity.ERROR),
        ("{{chosen_flow}}", "DYNAMIC_REFERENCE", PreflightSeverity.WARNING),
    ],
)
def test_missing_and_dynamic_subflow_references(
    flow_name: str,
    code: str,
    severity: PreflightSeverity,
) -> None:
    report = preflight_flow(
        flow(
            "root",
            command("run_flow", params={"flow_name": flow_name}),
        ),
        db=FakeDatabase(),
    )

    assert_issue(
        report,
        code,
        severity,
        "commands[0]",
        parameter="flow_name",
    )


def test_direct_recursive_flow_call_is_an_error() -> None:
    root = flow("root", command("run_flow", params={"flow_name": "root"}))

    report = preflight_flow(root, db=FakeDatabase(flows=(root,)))

    assert_issue(
        report,
        "RECURSIVE_FLOW_CALL",
        PreflightSeverity.ERROR,
        "commands[0]",
        parameter="flow_name",
    )


def test_indirect_cycle_through_else_branch_is_an_error() -> None:
    root = flow("root", command("run_flow", params={"flow_name": "middle"}))
    middle = flow(
        "middle",
        command(
            "if_elapsed",
            else_children=[
                command("run_flow", step=3, params={"flow_name": "root"})
            ],
        ),
    )

    report = preflight_flow(root, db=FakeDatabase(flows=(root, middle)))

    assert_issue(
        report,
        "RECURSIVE_FLOW_CALL",
        PreflightSeverity.ERROR,
        "commands[0].else_children[0]",
        parameter="flow_name",
    )
    assert report.visited_flows == ("root", "middle")


def test_shared_acyclic_callee_is_not_mistaken_for_a_cycle() -> None:
    shared = flow("shared", command("wait", params={"ms": 1}))
    left = flow("left", command("run_flow", params={"flow_name": "shared"}))
    right = flow("right", command("run_flow", params={"flow_name": "shared"}))
    root = flow(
        "root",
        command("run_flow", params={"flow_name": "left"}),
        command("run_flow", step=2, params={"flow_name": "right"}),
    )

    report = preflight_flow(
        root,
        db=FakeDatabase(flows=(root, left, right, shared)),
    )

    assert not issues_with(report, "RECURSIVE_FLOW_CALL")
    assert not report.errors
    assert set(report.visited_flows) == {"root", "left", "right", "shared"}
    assert len(report.visited_flows) == 4
