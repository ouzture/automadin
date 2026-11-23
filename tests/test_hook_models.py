from uuid import uuid4

from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.models.hook import (
    AutomationHook,
    BrowserElementHookCondition,
    HookAutomaticScope,
    HookBinding,
    HookMatch,
    HookPhase,
    VisualImageHookCondition,
)


def test_legacy_flow_without_hooks_loads_with_empty_collection() -> None:
    flow = AutomationFlow.from_dict({"name": "Legacy", "commands": []})

    assert flow.hooks == []
    assert flow.to_dict()["hooks"] == []


def test_typed_hooks_and_command_bindings_round_trip() -> None:
    image_hook = AutomationHook(
        name="Captcha",
        description="Pause when the challenge is visible",
        condition=VisualImageHookCondition(
            ("captcha.png",),
            threshold=0.87,
            search_settings={"search_region_mode": "full"},
        ),
        actions=[FlowCommand("user_checkpoint")],
        automatic_scope=HookAutomaticScope.ALL_COMPATIBLE_COMMANDS,
        automatic_phases=(HookPhase.BEFORE, HookPhase.AFTER),
    )
    browser_hook = AutomationHook(
        name="Session expired",
        condition=BrowserElementHookCondition(
            {"strategy": "text", "value": "Sign in"},
            HookMatch.NOT_FOUND,
        ),
    )
    command = FlowCommand(
        "click_image",
        hook_bindings=[HookBinding(image_hook.hook_id, (HookPhase.AFTER,))],
    )
    flow = AutomationFlow("Hooked", [command], hooks=[image_hook, browser_hook])

    assert AutomationFlow.from_dict(flow.to_dict()) == flow


def test_flow_deep_copy_rewrites_hook_bindings_and_check_hooks() -> None:
    hook = AutomationHook(
        name="Interruption",
        condition=VisualImageHookCondition(("warning.png",)),
        actions=[FlowCommand("wait")],
    )
    bound = FlowCommand(
        "click_image",
        hook_bindings=[HookBinding(hook.hook_id)],
    )
    explicit = FlowCommand("check_hooks", params={"hooks": [hook.hook_id]})
    original = AutomationFlow("Original", [bound, explicit], hooks=[hook])

    copied = original.deep_copy(name="Copy")

    assert copied.name == "Copy"
    assert copied.flow_id != original.flow_id
    assert copied.hooks[0].hook_id != hook.hook_id
    assert copied.commands[0].hook_bindings[0].hook_id == copied.hooks[0].hook_id
    assert copied.commands[1].params["hooks"] == [copied.hooks[0].hook_id]
    assert copied.hooks[0].actions[0].step_id != hook.actions[0].step_id


def test_binding_rejects_non_uuid_and_duplicate_phases() -> None:
    try:
        HookBinding("not-an-id")
    except ValueError as exc:
        assert "UUID" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("invalid hook binding ID was accepted")

    try:
        HookBinding(str(uuid4()), (HookPhase.BEFORE, HookPhase.BEFORE))
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("duplicate hook phases were accepted")
