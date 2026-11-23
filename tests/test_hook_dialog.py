from __future__ import annotations

from PySide6.QtWidgets import QApplication, QComboBox, QPushButton, QScrollArea, QWidget

from automadin.execution.handlers.registry import get_default_params, load_command_types
from automadin.models.builtin_scope import IMAGE_MATCH_BUILTIN_TYPES
from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.models.hook import HookType, VisualImageHookCondition, AutomationHook
from automadin.ui.flow_editor.hook_dialog import HookEditDialog, _CommandEditDialog


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_hook_action_editor_builds_and_finds_nested_switch_tree() -> None:
    _app()
    dialog = HookEditDialog(
        AutomationFlow("Hooks"),
        load_command_types(),
        object(),
        hook_type=HookType.BROWSER_ELEMENT,
    )
    switch = dialog._make_command("switch_value")
    nested = FlowCommand("send_notification")
    switch.children[0].children.append(nested)
    dialog._actions.append(switch)

    assert [branch.id for branch in switch.children] == [
        "switch_case",
        "switch_default",
    ]
    assert dialog._find_action(nested.step_id) == (
        switch.children[0].children,
        0,
    )
    dialog._refresh_actions(switch.step_id)
    dialog._add_child_action()
    assert [branch.id for branch in switch.children] == [
        "switch_case",
        "switch_case",
        "switch_default",
    ]
    dialog._refresh_actions(nested.step_id)
    assert dialog._selected_action() is nested


def test_hook_condition_test_callback_receives_action_free_hook() -> None:
    _app()
    observed = []
    dialog = HookEditDialog(
        AutomationFlow("Hooks"),
        load_command_types(),
        object(),
        hook_type=HookType.BROWSER_ELEMENT,
        test_condition=observed.append,
    )
    dialog._condition_values["locator"] = "#dialog"
    button = next(
        button
        for button in dialog.findChildren(QPushButton)
        if button.text().startswith("Test condition now")
    )

    button.click()

    assert len(observed) == 1
    assert observed[0].actions == []
    assert observed[0].condition.locator == "#dialog"


def test_hook_command_dialog_reuses_guided_presentation_inside_scroll_area() -> None:
    _app()
    command_types = load_command_types()
    command = FlowCommand(
        "click_image",
        params=get_default_params(command_types["click_image"]),
    )
    parent = QWidget()
    dialog = _CommandEditDialog(
        command,
        command_types,
        AutomationFlow("Hooks"),
        object(),
        parent,
        action_roots=[command],
    )

    scroll = dialog.findChild(QScrollArea, "hookCommandScrollArea")
    assert scroll is not None
    assert scroll.widgetResizable()
    assert dialog.command_card.findChild(QWidget, "guidedSection_target") is not None
    assert dialog.command_card.findChild(QWidget, "guidedSection_scope") is not None
    assert dialog.command_card.findChild(QWidget, "guidedSection_failure") is not None


def test_visual_hook_action_editor_offers_scoped_match_builtins() -> None:
    _app()
    command_types = load_command_types()
    flow = AutomationFlow("Hooks", value_model_version=1)
    command = FlowCommand(
        "click_position",
        params=get_default_params(command_types["click_position"]),
    )
    parent = QWidget()
    dialog = _CommandEditDialog(
        command,
        command_types,
        flow,
        object(),
        parent,
        action_roots=[command],
        base_builtin_types=dict(IMAGE_MATCH_BUILTIN_TYPES),
    )

    options = {
        combo.itemData(index)
        for combo in dialog.findChildren(QComboBox)
        for index in range(combo.count())
    }
    assert ("builtin", "_match_x") in options
    assert ("builtin", "_match_y") in options


def test_visual_hook_condition_edits_and_preserves_search_scope() -> None:
    _app()
    hook = AutomationHook(
        "Scoped",
        VisualImageHookCondition(
            ("target.png",),
            search_settings={
                "search_region_mode": "fixed",
                "search_start_x": 10,
                "search_start_y": 20,
                "future_setting": "preserve-me",
            },
        ),
    )
    dialog = HookEditDialog(
        AutomationFlow("Hooks", hooks=[hook]),
        load_command_types(),
        object(),
        hook=hook,
    )

    assert dialog.findChild(QScrollArea, "hookConditionScrollArea") is not None
    dialog._condition_values["search_end_x"] = 500
    condition = dialog._condition()

    assert isinstance(condition, VisualImageHookCondition)
    assert condition.search_settings["search_region_mode"] == "fixed"
    assert condition.search_settings["search_end_x"] == 500
    assert condition.search_settings["future_setting"] == "preserve-me"
