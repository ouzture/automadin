from __future__ import annotations

from automadin.execution.handlers.registry import load_command_types, validate_command_types
from automadin.models.builtin_scope import builtin_types_for_step
from automadin.models.flow import FlowCommand
from automadin.models.typed_value import ValueType


def test_builtin_scope_follows_children_not_else_or_siblings() -> None:
    root = FlowCommand("comment")
    nested = FlowCommand("update_variable")
    sibling_child = FlowCommand("update_variable")
    else_child = FlowCommand("update_variable")
    outside_sibling = FlowCommand("update_variable")
    group = FlowCommand("group", children=[nested])
    loop = FlowCommand(
        "for_each",
        children=[group, sibling_child],
        else_children=[else_child],
    )
    commands = [root, loop, outside_sibling]
    definitions = load_command_types()

    expected = {"item": ValueType.TEXT, "index": ValueType.INTEGER}
    assert builtin_types_for_step(commands, nested.step_id, definitions) == expected
    assert (
        builtin_types_for_step(commands, sibling_child.step_id, definitions)
        == expected
    )
    assert builtin_types_for_step(commands, root.step_id, definitions) == {}
    assert builtin_types_for_step(commands, else_child.step_id, definitions) == {}
    assert builtin_types_for_step(commands, outside_sibling.step_id, definitions) == {}
    assert builtin_types_for_step(commands, "missing", definitions) == {}


def test_nested_loop_scope_unions_declared_builtins() -> None:
    target = FlowCommand("update_variable")
    inner = FlowCommand("for_each", children=[target])
    outer = FlowCommand("for", children=[inner])

    assert builtin_types_for_step(
        [outer], target.step_id, load_command_types()
    ) == {"index": ValueType.INTEGER, "item": ValueType.TEXT}


def test_browser_element_builtins_are_child_only() -> None:
    child = FlowCommand("click_element", params={"target": "current_element"})
    outside = FlowCommand("click_element", params={"target": "current_element"})
    loop = FlowCommand("for_each_browser_element", children=[child])
    definitions = load_command_types()

    assert builtin_types_for_step([loop, outside], child.step_id, definitions) == {
        "element_index": ValueType.INTEGER,
        "element_count": ValueType.INTEGER,
    }
    assert builtin_types_for_step([loop, outside], outside.step_id, definitions) == {}


def test_image_condition_match_coordinates_are_child_only() -> None:
    definitions = load_command_types()
    expected = {
        "_match_x": ValueType.INTEGER,
        "_match_y": ValueType.INTEGER,
    }

    for command_id in ("if_image", "while", "win_if_image", "win_while"):
        child = FlowCommand("swipe")
        outside = FlowCommand("swipe")
        else_child = FlowCommand("swipe")
        condition = FlowCommand(
            command_id,
            children=[child],
            else_children=[else_child],
        )

        assert builtin_types_for_step(
            [condition, outside], child.step_id, definitions
        ) == expected
        assert builtin_types_for_step(
            [condition, outside], else_child.step_id, definitions
        ) == {}
        assert builtin_types_for_step(
            [condition, outside], outside.step_id, definitions
        ) == {}


def test_registry_rejects_unknown_child_builtin_name() -> None:
    entries = [
        {
            "id": "example",
            "childBuiltins": ["mystery"],
            "params": [],
        }
    ]

    try:
        validate_command_types(entries)
    except ValueError as exc:
        assert "unknown child Built-ins: mystery" in str(exc)
    else:
        raise AssertionError("unknown Built-in metadata was accepted")
