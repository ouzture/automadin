from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox

from automadin.execution.context import FlowContext
from automadin.execution.coordinates import resolve_point
from automadin.execution.flow_values import FlowInvocationValues, resolve_flow_inputs
from automadin.execution.handlers.registry import load_command_types
from automadin.execution.handlers.detection import handle_find_image, handle_screenshot
from automadin.execution.handlers.actions import handle_click_image
from automadin.execution.handlers.actions import handle_press_keycode
from automadin.execution.handlers.type_text import handle_type_text
from automadin.execution.executor import FlowExecutor
from automadin.execution.handlers.browser_elements import (
    handle_click_element,
    handle_input_fill,
    handle_wait_for_selector,
)
from automadin.execution.handlers.browser_ext import handle_wait_for_navigation
from automadin.execution.handlers.control import (
    handle_for,
    handle_if_elapsed,
    handle_if_image,
    handle_while,
)
from automadin.execution.handlers.windows import (
    handle_win_ensure_app,
    handle_win_click,
    handle_win_click_image,
    handle_win_drag,
    handle_win_key,
    handle_win_scroll,
    handle_win_type,
    handle_win_if_image,
    handle_win_while,
)
from automadin.execution.handlers.visual import (
    _source_point,
    handle_click_dynamic_target,
)
from automadin.execution.handlers.android_apps import (
    handle_android_install_apk,
    handle_android_launch_app,
    handle_android_open_deep_link,
    handle_android_restart_app,
    handle_android_start_activity,
)
from automadin.execution.handlers.wait import handle_random_wait, handle_wait
from automadin.execution.handlers.tab import (
    handle_select_browser_page,
    handle_switch_tab,
)
from automadin.models.device import (
    AndroidDevice,
    BrowserDevice,
    BrowserDeviceConfig,
    BrowserEngine,
    DeviceSession,
    WindowsDevice,
)
from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    AutomationFlow,
    FlowCommand,
)
from automadin.models.flow_value import FlowInputDefinition, FlowVariableDefinition
from automadin.models.typed_value import ValueType
from automadin.services.flow_preflight import preflight_flow
from automadin.ui.flow_editor.command_card import CommandCard
from automadin.ui.flow_editor.param_editor import ParamEditorWidget

_DURATION_INPUT_ID = "10000000-0000-4000-8000-000000000001"
_INTEGER_VARIABLE_ID = "20000000-0000-4000-8000-000000000001"
_DURATION_VARIABLE_ID = "20000000-0000-4000-8000-000000000002"
_NUMBER_VARIABLE_ID = "20000000-0000-4000-8000-000000000003"
_BOOLEAN_VARIABLE_ID = "20000000-0000-4000-8000-000000000004"


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _configured_value_reference_pickers(card: CommandCard) -> list[QComboBox]:
    """Ignore optional timing pickers with no saved value yet."""
    return [
        picker
        for picker in card.findChildren(QComboBox, "valueReferencePicker")
        if picker.currentData() is not None
    ]


def _value_reference_pickers_by_field(card: CommandCard) -> dict[str, QComboBox]:
    pickers: dict[str, QComboBox] = {}
    for picker in card.findChildren(QComboBox, "valueReferencePicker"):
        widget = picker.parentWidget()
        while widget is not None:
            object_name = widget.objectName()
            if object_name.startswith("commandField_"):
                pickers[object_name.removeprefix("commandField_")] = picker
                break
            widget = widget.parentWidget()
    return pickers


def _assert_visible_picker_fields(
    card: CommandCard,
    expected_types: dict[str, str],
) -> None:
    pickers = _value_reference_pickers_by_field(card)

    assert len(pickers) == len(expected_types)
    assert set(pickers) == set(expected_types)
    for field_name, expected_type in expected_types.items():
        picker = pickers[field_name]
        assert ("literal", expected_type) in [
            picker.itemData(index) for index in range(picker.count())
        ]


def _flow(command: FlowCommand) -> AutomationFlow:
    return AutomationFlow(
        "Duration references",
        [command],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        inputs=[
            FlowInputDefinition(
                "timeout",
                ValueType.DURATION_MS,
                500,
                id=_DURATION_INPUT_ID,
            )
        ],
        variables=[
            FlowVariableDefinition(
                "count",
                ValueType.INTEGER,
                1,
                id=_INTEGER_VARIABLE_ID,
            ),
            FlowVariableDefinition(
                "delay",
                ValueType.DURATION_MS,
                750,
                id=_DURATION_VARIABLE_ID,
            ),
            FlowVariableDefinition(
                "threshold",
                ValueType.NUMBER,
                0.9,
                id=_NUMBER_VARIABLE_ID,
            ),
            FlowVariableDefinition(
                "use_color",
                ValueType.BOOLEAN,
                True,
                id=_BOOLEAN_VARIABLE_ID,
            ),
        ],
    )


def test_wait_metadata_exposes_duration_references() -> None:
    wait = load_command_types()["wait"]
    ms = next(param for param in wait["params"] if param["name"] == "ms")

    assert ms["supportsValueReference"] is True
    assert ms["allowed_value_types"] == ["duration_ms"]

    wait_for_selector = load_command_types()["wait_for_selector"]
    timeout = next(
        param for param in wait_for_selector["params"] if param["name"] == "timeout"
    )
    assert timeout["supportsValueReference"] is True
    assert timeout["allowed_value_types"] == ["duration_ms"]

    wait_for_navigation = load_command_types()["wait_for_navigation"]
    navigation_timeout = next(
        param
        for param in wait_for_navigation["params"]
        if param["name"] == "timeout"
    )
    assert navigation_timeout["supportsValueReference"] is True
    assert navigation_timeout["allowed_value_types"] == ["duration_ms"]

    click_element = load_command_types()["click_element"]
    click_timeout = next(
        param for param in click_element["params"] if param["name"] == "timeout"
    )
    assert click_timeout["supportsValueReference"] is True
    assert click_timeout["allowed_value_types"] == ["duration_ms"]

    if_image = load_command_types()["if_image"]
    if_timeout = next(
        param for param in if_image["params"] if param["name"] == "timeout"
    )
    assert if_timeout["supportsValueReference"] is True
    assert if_timeout["allowed_value_types"] == ["duration_ms"]

    while_command = load_command_types()["while"]
    while_timeout = next(
        param for param in while_command["params"] if param["name"] == "timeout"
    )
    assert while_timeout["supportsValueReference"] is True
    assert while_timeout["allowed_value_types"] == ["duration_ms"]

    win_if_image = load_command_types()["win_if_image"]
    win_timeout = next(
        param for param in win_if_image["params"] if param["name"] == "timeout"
    )
    assert win_timeout["supportsValueReference"] is True
    assert win_timeout["allowed_value_types"] == ["duration_ms"]

    win_while = load_command_types()["win_while"]
    win_while_timeout = next(
        param for param in win_while["params"] if param["name"] == "timeout"
    )
    assert win_while_timeout["supportsValueReference"] is True
    assert win_while_timeout["allowed_value_types"] == ["duration_ms"]

    android_install = load_command_types()["android_install_apk"]
    install_timeout = next(
        param for param in android_install["params"] if param["name"] == "timeout"
    )
    assert install_timeout["supportsValueReference"] is True
    assert install_timeout["allowed_value_types"] == ["duration_ms"]

    android_launch = load_command_types()["android_launch_app"]
    launch_timeout = next(
        param for param in android_launch["params"] if param["name"] == "timeout"
    )
    assert launch_timeout["supportsValueReference"] is True
    assert launch_timeout["allowed_value_types"] == ["duration_ms"]

    android_restart = load_command_types()["android_restart_app"]
    restart_timeout = next(
        param for param in android_restart["params"] if param["name"] == "timeout"
    )
    assert restart_timeout["supportsValueReference"] is True
    assert restart_timeout["allowed_value_types"] == ["duration_ms"]

    android_activity = load_command_types()["android_start_activity"]
    activity_timeout = next(
        param for param in android_activity["params"] if param["name"] == "timeout"
    )
    assert activity_timeout["supportsValueReference"] is True
    assert activity_timeout["allowed_value_types"] == ["duration_ms"]


    android_deep_link = load_command_types()["android_open_deep_link"]
    deep_link_timeout = next(
        param for param in android_deep_link["params"] if param["name"] == "timeout"
    )
    assert deep_link_timeout["supportsValueReference"] is True
    assert deep_link_timeout["allowed_value_types"] == ["duration_ms"]

    if_image = load_command_types()["if_image"]
    threshold = next(
        param for param in if_image["params"] if param["name"] == "threshold"
    )
    assert threshold["supportsValueReference"] is True
    assert threshold["allowed_value_types"] == ["number"]

    while_command = load_command_types()["while"]
    while_threshold = next(
        param for param in while_command["params"] if param["name"] == "threshold"
    )
    assert while_threshold["supportsValueReference"] is True
    assert while_threshold["allowed_value_types"] == ["number"]

    if_color_tolerance = next(
        param
        for param in if_image["params"]
        if param["name"] == "color_tolerance"
    )
    assert if_color_tolerance["supportsValueReference"] is True
    assert if_color_tolerance["allowed_value_types"] == ["number"]

    while_color_tolerance = next(
        param
        for param in while_command["params"]
        if param["name"] == "color_tolerance"
    )
    assert while_color_tolerance["supportsValueReference"] is True
    assert while_color_tolerance["allowed_value_types"] == ["number"]

    for command_id in (
        "find_image",
        "click_image",
        "if_image",
        "while",
        "win_click_image",
        "win_drag_image",
        "win_if_image",
        "win_while",
    ):
        command_type = load_command_types()[command_id]
        for name in ("threshold", "color_tolerance"):
            parameter = next(
                param for param in command_type["params"] if param["name"] == name
            )
            assert parameter["supportsValueReference"] is True
            assert parameter["allowed_value_types"] == ["number"]
        color_match = next(
            param for param in command_type["params"] if param["name"] == "color_match"
        )
        assert color_match["supportsValueReference"] is True
        assert color_match["allowed_value_types"] == ["boolean"]

        for name in (
            "search_start_x",
            "search_start_y",
            "search_end_x",
            "search_end_y",
            "search_offset_x",
            "search_offset_y",
            "search_region_width",
            "search_region_height",
        ):
            region_coordinate = next(
                param for param in command_type["params"] if param["name"] == name
            )
            assert region_coordinate["supportsValueReference"] is True
            assert region_coordinate["allowed_value_types"] == ["number"]
        region_mode = next(
            param
            for param in command_type["params"]
            if param["name"] == "search_region_mode"
        )
        assert region_mode["options"] == ["full", "fixed", "image_anchor"]

    find_image = load_command_types()["find_image"]
    for name in ("threshold", "color_tolerance"):
        parameter = next(
            param for param in find_image["params"] if param["name"] == name
        )
        assert parameter["supportsValueReference"] is True
        assert parameter["allowed_value_types"] == ["number"]
    find_color_match = next(
        param for param in find_image["params"] if param["name"] == "color_match"
    )
    assert find_color_match["supportsValueReference"] is True
    assert find_color_match["allowed_value_types"] == ["boolean"]

    for command_id in ("press_keycode", "win_key"):
        command_type = load_command_types()[command_id]
        pause = next(
            param for param in command_type["params"] if param["name"] == "pause_ms"
        )
        assert pause["supportsValueReference"] is True
        assert pause["allowed_value_types"] == ["duration_ms"]

    for command_id in ("type_text", "win_type"):
        command_type = load_command_types()[command_id]
        for name in ("min_delay", "max_delay", "delay_ms"):
            delay = next(
                param for param in command_type["params"] if param["name"] == name
            )
            assert delay["supportsValueReference"] is True
            assert delay["allowed_value_types"] == ["duration_ms"]

    if_elapsed = load_command_types()["if_elapsed"]
    min_wait = next(
        param for param in if_elapsed["params"] if param["name"] == "min_wait_time"
    )
    assert min_wait["supportsValueReference"] is True
    assert min_wait["allowed_value_types"] == ["duration_ms"]

    for command_id in ("for", "for_each"):
        command_type = load_command_types()[command_id]
        min_wait = next(
            param
            for param in command_type["params"]
            if param["name"] == "min_wait_time"
        )
        assert min_wait["supportsValueReference"] is True
        assert min_wait["allowed_value_types"] == ["duration_ms"]

    for command_id, name in (
        ("screenshot", "pause_ms"),
        ("screenshot", "min_wait_time"),
        ("wait", "pause_ms"),
        ("random_wait", "pause_ms"),
    ):
        command_type = load_command_types()[command_id]
        timing = next(
            param for param in command_type["params"] if param["name"] == name
        )
        assert timing["supportsValueReference"] is True
        assert timing["allowed_value_types"] == ["duration_ms"]

    for command_id in ("navigate_to", "go_back", "go_forward", "reload_page"):
        command_type = load_command_types()[command_id]
        for name in ("pause_ms", "min_wait_time"):
            timing = next(
                param for param in command_type["params"] if param["name"] == name
            )
            assert timing["supportsValueReference"] is True
            assert timing["allowed_value_types"] == ["duration_ms"]

    for command_id in ("open_new_tab", "close_tab", "close_all_tabs", "switch_tab"):
        command_type = load_command_types()[command_id]
        for name in ("pause_ms", "min_wait_time"):
            timing = next(
                param for param in command_type["params"] if param["name"] == name
            )
            assert timing["supportsValueReference"] is True
            assert timing["allowed_value_types"] == ["duration_ms"]

    switch_tab = load_command_types()["switch_tab"]
    index = next(param for param in switch_tab["params"] if param["name"] == "index")
    assert index["supportsValueReference"] is True
    assert index["allowed_value_types"] == ["integer"]

    select_page = load_command_types()["select_browser_page"]
    match_case = next(
        param for param in select_page["params"] if param["name"] == "match_case"
    )
    assert match_case["supportsValueReference"] is True
    assert match_case["allowed_value_types"] == ["boolean"]

    for command_id in (
        "input_fill",
        "extract_text",
        "evaluate_js",
        "assert_visible",
        "assert_url_contains",
    ):
        command_type = load_command_types()[command_id]
        names = (
            ("min_delay", "max_delay", "delay_ms", "pause_ms", "min_wait_time")
            if command_id == "input_fill"
            else ("pause_ms", "min_wait_time")
        )
        for name in names:
            timing = next(
                param for param in command_type["params"] if param["name"] == name
            )
            assert timing["supportsValueReference"] is True
            assert timing["allowed_value_types"] == ["duration_ms"]

    win_click = load_command_types()["win_click"]
    for name in ("focus_delay_ms", "click_duration_ms", "restore_delay_ms"):
        timing = next(param for param in win_click["params"] if param["name"] == name)
        assert timing["supportsValueReference"] is True
        assert timing["allowed_value_types"] == ["duration_ms"]

    win_drag = load_command_types()["win_drag"]
    for name in ("drag_duration_ms", "focus_delay_ms", "restore_delay_ms"):
        timing = next(param for param in win_drag["params"] if param["name"] == name)
        assert timing["supportsValueReference"] is True
        assert timing["allowed_value_types"] == ["duration_ms"]
    drag_steps = next(param for param in win_drag["params"] if param["name"] == "drag_steps")
    assert drag_steps["supportsValueReference"] is True
    assert drag_steps["allowed_value_types"] == ["integer"]

    win_scroll = load_command_types()["win_scroll"]
    for name in ("x", "y", "amount"):
        scalar = next(param for param in win_scroll["params"] if param["name"] == name)
        assert scalar["supportsValueReference"] is True
        assert scalar["allowed_value_types"] == ["integer"]

    click_position = load_command_types()["click_position"]
    for name in ("x", "y"):
        coordinate = next(
            param for param in click_position["params"] if param["name"] == name
        )
        assert coordinate["supportsValueReference"] is True
        assert coordinate["allowed_value_types"] == ["number"]

    swipe = load_command_types()["swipe"]
    for name in ("start_x", "start_y", "end_x", "end_y"):
        coordinate = next(param for param in swipe["params"] if param["name"] == name)
        assert coordinate["supportsValueReference"] is True
        assert coordinate["allowed_value_types"] == ["number"]
    duration = next(param for param in swipe["params"] if param["name"] == "duration_ms")
    assert duration["supportsValueReference"] is True
    assert duration["allowed_value_types"] == ["duration_ms"]

    for command_id, names, expected_type in (
        ("click_image", ("xmargin", "ymargin"), "integer"),
        ("if_image", ("xmargin", "ymargin"), "integer"),
        ("win_click_image", ("xmargin", "ymargin"), "integer"),
        ("win_drag_image", ("xmargin", "ymargin", "xoffset", "yoffset"), "integer"),
        ("win_if_image", ("xmargin", "ymargin"), "integer"),
        ("win_while", ("xmargin", "ymargin"), "integer"),
    ):
        command_type = load_command_types()[command_id]
        for name in names:
            coordinate = next(
                param for param in command_type["params"] if param["name"] == name
            )
            assert coordinate["supportsValueReference"] is True
            assert coordinate["allowed_value_types"] == [expected_type]

    for command_id, names in (
        ("click_image", ("hold_duration_ms",)),
        ("if_image", ("hold_duration_ms",)),
        ("win_click_image", ("focus_delay_ms", "click_duration_ms", "restore_delay_ms")),
        ("win_drag_image", ("drag_duration_ms", "focus_delay_ms", "restore_delay_ms")),
        ("win_if_image", ("focus_delay_ms", "click_duration_ms", "restore_delay_ms")),
        ("win_while", ("focus_delay_ms", "click_duration_ms", "restore_delay_ms", "interval_ms")),
    ):
        command_type = load_command_types()[command_id]
        for name in names:
            timing = next(param for param in command_type["params"] if param["name"] == name)
            assert timing["supportsValueReference"] is True
            assert timing["allowed_value_types"] == ["duration_ms"]

    for command_id in ("if_image", "win_if_image", "win_while"):
        command_type = load_command_types()[command_id]
        name = "clickIfFound" if command_id == "if_image" else "click_if_found"
        click_if_found = next(param for param in command_type["params"] if param["name"] == name)
        assert click_if_found["supportsValueReference"] is True
        assert click_if_found["allowed_value_types"] == ["boolean"]

    for command_id in ("if_image", "while", "win_if_image", "win_while"):
        command_type = load_command_types()[command_id]
        names = {param["name"] for param in command_type["params"]}
        assert {"condition_unmet_behavior", "condition_unmet_message"} <= names

    count = next(param for param in load_command_types()["for"]["params"] if param["name"] == "count")
    assert count["supportsValueReference"] is True
    assert count["allowed_value_types"] == ["integer"]

    save_to_disk = next(
        param for param in load_command_types()["screenshot"]["params"] if param["name"] == "save_to_disk"
    )
    assert save_to_disk["supportsValueReference"] is True
    assert save_to_disk["allowed_value_types"] == ["boolean"]

    win_focus = next(
        param for param in load_command_types()["win_ensure_app"]["params"] if param["name"] == "focus"
    )
    assert win_focus["supportsValueReference"] is True
    assert win_focus["allowed_value_types"] == ["boolean"]

    random_wait = load_command_types()["random_wait"]
    for name in ("min_ms", "max_ms"):
        bound = next(param for param in random_wait["params"] if param["name"] == name)
        assert bound["supportsValueReference"] is True
        assert bound["allowed_value_types"] == ["duration_ms"]

    android_install = load_command_types()["android_install_apk"]
    for name in ("replace_existing", "grant_permissions"):
        option = next(param for param in android_install["params"] if param["name"] == name)
        assert option["supportsValueReference"] is True
        assert option["allowed_value_types"] == ["boolean"]

    win_ensure = load_command_types()["win_ensure_app"]
    ensure_timeout = next(
        param for param in win_ensure["params"] if param["name"] == "timeout"
    )
    assert ensure_timeout["supportsValueReference"] is True
    assert ensure_timeout["allowed_value_types"] == ["duration_ms"]


def test_global_timing_metadata_exposes_duration_references() -> None:
    command_types = load_command_types()
    for command_id, command_type in command_types.items():
        for param in command_type.get("params", []):
            if param["name"] not in {"pause_ms", "min_wait_time"}:
                continue
            assert param["type"] == "int", (command_id, param["name"])
            assert param["supportsValueReference"] is True, (
                command_id,
                param["name"],
            )
            assert param["allowed_value_types"] == ["duration_ms"], (
                command_id,
                param["name"],
            )


def test_wait_editor_keeps_legacy_literal_and_offers_duration_sources() -> None:
    _app()
    command = FlowCommand("wait", params={"ms": 500})
    flow = _flow(command)
    card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))
    picker = card.findChild(QComboBox, "valueReferencePicker")

    assert picker is not None
    assert picker.currentData() == ("literal", "duration_ms")
    items = [picker.itemData(index) for index in range(picker.count())]
    assert ("input", _DURATION_INPUT_ID) in items
    assert ("variable", _DURATION_VARIABLE_ID) in items
    assert ("variable", _INTEGER_VARIABLE_ID) not in items
    card.deleteLater()


def test_browser_wait_editors_expose_duration_timeout_picker() -> None:
    _app()
    for command_id, params in (
        ("wait_for_selector", {"locator": "#ready", "timeout": 5000}),
        ("wait_for_navigation", {"timeout": 30000}),
    ):
        command = FlowCommand(command_id, params=params)
        card = CommandCard(
            command,
            load_command_types(),
            SimpleNamespace(_flow=_flow(command)),
        )
        try:
            pickers = card.findChildren(QComboBox, "valueReferencePicker")
            assert len(pickers) == 3, command_id
            assert any(
                picker.currentData() == ("literal", "duration_ms")
                for picker in pickers
            ), command_id
        finally:
            card.deleteLater()


def test_click_element_editor_exposes_duration_timeout_picker() -> None:
    _app()
    command = FlowCommand(
        "click_element",
        params={"locator": "#save", "timeout": 5000},
    )
    flow = _flow(command)
    card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))
    pickers = card.findChildren(QComboBox, "valueReferencePicker")

    assert len(pickers) == 4
    assert [picker.currentData() for picker in pickers].count(
        ("literal", "duration_ms")
    ) == 1
    assert [picker.currentData() for picker in pickers].count(
        ("literal", "number")
    ) == 2
    assert [picker.currentData() for picker in pickers].count(
        ("literal", "boolean")
    ) == 1
    card.deleteLater()


def test_image_condition_editors_expose_typed_picker_contracts() -> None:
    _app()
    cases = (
        (
            "if_image",
            {
                "threshold": "number",
                "color_match": "boolean",
                "clickIfFound": "boolean",
                "timeout": "duration_ms",
                "pause_ms": "duration_ms",
                "min_wait_time": "duration_ms",
            },
        ),
        (
            "while",
            {
                "threshold": "number",
                "color_match": "boolean",
                "timeout": "duration_ms",
                "pause_ms": "duration_ms",
                "min_wait_time": "duration_ms",
            },
        ),
    )
    for command_id, expected_fields in cases:
        command = FlowCommand(
            command_id,
            params={"images": ["target"], "threshold": 0.9, "timeout": 30000},
        )
        card = CommandCard(
            command,
            load_command_types(),
            SimpleNamespace(_flow=_flow(command)),
        )
        try:
            _assert_visible_picker_fields(card, expected_fields)
        finally:
            card.deleteLater()


def test_win_if_image_editor_exposes_duration_timeout_picker() -> None:
    _app()
    command = FlowCommand(
        "win_if_image",
        params={"images": ["target"], "timeout": 30000},
    )
    flow = _flow(command)
    card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))
    _assert_visible_picker_fields(
        card,
        {
            "threshold": "number",
            "color_match": "boolean",
            "click_if_found": "boolean",
            "xmargin": "integer",
            "ymargin": "integer",
            "timeout": "duration_ms",
            "pause_ms": "duration_ms",
            "min_wait_time": "duration_ms",
        },
    )
    card.deleteLater()


def test_win_while_editor_exposes_duration_timeout_picker() -> None:
    _app()
    command = FlowCommand(
        "win_while",
        params={"images": ["target"], "timeout": 30000},
    )
    flow = _flow(command)
    card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))
    _assert_visible_picker_fields(
        card,
        {
            "threshold": "number",
            "color_match": "boolean",
            "click_if_found": "boolean",
            "xmargin": "integer",
            "ymargin": "integer",
            "timeout": "duration_ms",
            "interval_ms": "duration_ms",
            "pause_ms": "duration_ms",
            "min_wait_time": "duration_ms",
        },
    )
    card.deleteLater()


def test_click_image_editor_exposes_number_match_pickers() -> None:
    _app()
    command = FlowCommand(
        "click_image",
        params={"images": ["target"], "threshold": 0.9, "color_tolerance": 0.3},
    )
    flow = _flow(command)
    card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))
    _assert_visible_picker_fields(
        card,
        {
            "threshold": "number",
            "color_match": "boolean",
            "hold_duration_ms": "duration_ms",
            "xmargin": "integer",
            "ymargin": "integer",
            "pause_ms": "duration_ms",
            "min_wait_time": "duration_ms",
        },
    )
    card.deleteLater()


def test_find_image_editor_exposes_image_scalar_pickers() -> None:
    _app()
    command = FlowCommand(
        "find_image",
        params={"images": ["target"], "threshold": 0.9, "color_tolerance": 0.3},
    )
    flow = _flow(command)
    card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))
    _assert_visible_picker_fields(
        card,
        {
            "threshold": "number",
            "color_match": "boolean",
            "pause_ms": "duration_ms",
            "min_wait_time": "duration_ms",
        },
    )
    card.deleteLater()


def test_swipe_editor_exposes_if_image_match_coordinates_in_then_branch() -> None:
    _app()
    swipe = FlowCommand(
        "swipe",
        params={
            "coordinate_unit": "pixels",
            "start_x": 0,
            "start_y": 0,
            "end_x": 100,
            "end_y": 100,
        },
    )
    condition = FlowCommand(
        "if_image",
        params={"images": ["target"]},
        children=[swipe],
    )
    flow = _flow(condition)
    card = CommandCard(swipe, load_command_types(), SimpleNamespace(_flow=flow))

    try:
        pickers = _value_reference_pickers_by_field(card)
        for field_name in ("start_x", "start_y", "end_x", "end_y"):
            options = [
                pickers[field_name].itemData(index)
                for index in range(pickers[field_name].count())
            ]
            assert ("builtin", "_match_x") in options
            assert ("builtin", "_match_y") in options
    finally:
        card.deleteLater()


def test_key_editors_expose_duration_pause_picker() -> None:
    _app()
    for command_id in ("press_keycode", "win_key"):
        command = FlowCommand(
            command_id,
            params={"keys": ["Enter"], "pause_ms": 250},
        )
        card = CommandCard(
            command,
            load_command_types(),
            SimpleNamespace(_flow=_flow(command)),
        )
        try:
            pickers = card.findChildren(QComboBox, "valueReferencePicker")
            assert len(pickers) == 2, command_id
            assert pickers[0].currentData() == ("literal", "duration_ms"), command_id
        finally:
            card.deleteLater()


def test_text_entry_editors_expose_duration_delay_picker() -> None:
    _app()
    for command_id in ("type_text", "win_type"):
        command = FlowCommand(
            command_id,
            params={"text": "hello", "delay_ms": 250},
        )
        card = CommandCard(
            command,
            load_command_types(),
            SimpleNamespace(_flow=_flow(command)),
        )
        try:
            pickers = card.findChildren(QComboBox, "valueReferencePicker")
            assert len(pickers) == 5, command_id
            for picker in pickers[:2]:
                assert ("literal", "duration_ms") in [
                    picker.itemData(index) for index in range(picker.count())
                ], command_id
            assert any(
                picker.currentData() == ("literal", "duration_ms")
                for picker in pickers
            ), command_id
        finally:
            card.deleteLater()


def test_if_elapsed_editor_exposes_duration_min_wait_picker() -> None:
    _app()
    command = FlowCommand("if_elapsed", params={"min_wait_time": 1000})
    flow = _flow(command)
    card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))
    pickers = card.findChildren(QComboBox, "valueReferencePicker")

    assert len(pickers) == 2
    assert any(picker.currentData() == ("literal", "duration_ms") for picker in pickers)
    card.deleteLater()


def test_for_and_for_each_editors_expose_duration_min_wait_pickers() -> None:
    for command_id, params in (
        ("for", {"count": 2, "min_wait_time": 1000}),
        ("for_each", {"values": ["a"], "min_wait_time": 1000}),
    ):
        command = FlowCommand(command_id, params=params)
        flow = _flow(command)
        card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))
        pickers = card.findChildren(QComboBox, "valueReferencePicker")

        expected_count = 3 if command_id == "for" else 2
        assert len(pickers) == expected_count
        assert any(
            ("literal", "duration_ms") in [
                picker.itemData(index) for index in range(picker.count())
            ]
            for picker in pickers
        )
        card.deleteLater()


def test_timing_commands_expose_duration_pause_pickers() -> None:
    for command_id, params in (
        ("screenshot", {"pause_ms": 100}),
        ("wait", {"ms": 500, "pause_ms": 100}),
        ("random_wait", {"min_ms": 100, "max_ms": 200, "pause_ms": 100}),
    ):
        command = FlowCommand(command_id, params=params)
        flow = _flow(command)
        card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))
        pickers = card.findChildren(QComboBox, "valueReferencePicker")

        assert pickers
        assert any(
            ("literal", "duration_ms") in [
                picker.itemData(index) for index in range(picker.count())
            ]
            for picker in pickers
        )
        card.deleteLater()


def test_browser_navigation_exposes_duration_timing_pickers() -> None:
    for command_id, params in (
        ("navigate_to", {"url": "https://example.com", "pause_ms": 100, "min_wait_time": 200}),
        ("go_back", {"pause_ms": 100, "min_wait_time": 200}),
        ("go_forward", {"pause_ms": 100, "min_wait_time": 200}),
        ("reload_page", {"pause_ms": 100, "min_wait_time": 200}),
    ):
        command = FlowCommand(command_id, params=params)
        flow = _flow(command)
        card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))
        pickers = card.findChildren(QComboBox, "valueReferencePicker")

        assert len(pickers) == 3
        assert all(
            picker.currentData() == ("literal", "duration_ms") for picker in pickers
        )
        card.deleteLater()


def test_browser_tab_commands_expose_duration_timing_pickers() -> None:
    for command_id, params in (
        ("open_new_tab", {"url": "https://example.com", "pause_ms": 100, "min_wait_time": 200}),
        ("close_tab", {"pause_ms": 100, "min_wait_time": 200}),
        ("close_all_tabs", {"pause_ms": 100, "min_wait_time": 200}),
        ("switch_tab", {"index": 0, "pause_ms": 100, "min_wait_time": 200}),
    ):
        command = FlowCommand(command_id, params=params)
        flow = _flow(command)
        card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))
        pickers = card.findChildren(QComboBox, "valueReferencePicker")

        if command_id == "open_new_tab":
            expected_types = ["duration_ms", "duration_ms", "duration_ms"]
        elif command_id == "switch_tab":
            expected_types = ["integer", "duration_ms", "duration_ms"]
        else:
            expected_types = ["duration_ms", "duration_ms"]
        assert [picker.currentData()[1] for picker in pickers] == expected_types
        assert all(picker.currentData()[0] == "literal" for picker in pickers)
        card.deleteLater()


def test_browser_tab_selection_exposes_typed_scalar_pickers() -> None:
    for command_id, params, expected_type in (
        ("switch_tab", {"index": 0}, "integer"),
        (
            "select_browser_page",
            {"strategy": "url_contains", "value": "example", "match_case": False},
            "boolean",
        ),
    ):
        command = FlowCommand(command_id, params=params)
        flow = _flow(command)
        card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))
        pickers = card.findChildren(QComboBox, "valueReferencePicker")

        if command_id == "switch_tab":
            assert len(pickers) == 3
            assert pickers[0].currentData() == ("literal", "integer")
        else:
            assert len(pickers) == 1
            assert pickers[0].currentData() == ("literal", expected_type)
        card.deleteLater()


def test_switch_tab_runtime_resolves_integer_reference() -> None:
    flow = _flow(FlowCommand("switch_tab"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {}),
    )
    port = SimpleNamespace(switch_tab=Mock())
    context = FlowContext(
        DeviceSession(data={"browser_data": port}),
        flow_values=values,
    )

    handle_switch_tab(
        FlowCommand(
            "switch_tab",
            params={"index": {"kind": "variable", "id": _INTEGER_VARIABLE_ID}},
        ),
        context,
    )

    port.switch_tab.assert_called_once_with(1)


def test_select_browser_page_runtime_resolves_boolean_reference() -> None:
    flow = _flow(FlowCommand("select_browser_page"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {}),
    )
    port = SimpleNamespace(select_page=Mock())
    context = FlowContext(
        DeviceSession(data={"browser_data": port}),
        flow_values=values,
    )

    handle_select_browser_page(
        FlowCommand(
            "select_browser_page",
            params={
                "strategy": "newest",
                "match_case": {"kind": "variable", "id": _BOOLEAN_VARIABLE_ID},
            },
        ),
        context,
    )

    port.select_page.assert_called_once_with("newest", "", match_case=True)


def test_input_fill_editor_exposes_duration_delay_and_timing_pickers() -> None:
    command = FlowCommand(
        "input_fill",
        params={
            "locator": "#name",
            "value": "Ada",
            "min_delay": 100,
            "max_delay": 200,
            "delay_ms": 150,
            "pause_ms": 250,
            "min_wait_time": 300,
        },
    )
    flow = _flow(command)
    card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))
    pickers = card.findChildren(QComboBox, "valueReferencePicker")

    assert len(pickers) == 5
    assert all(
        picker.currentData() == ("literal", "duration_ms") for picker in pickers
    )
    card.deleteLater()


def test_input_fill_runtime_resolves_duration_delay_references() -> None:
    flow = _flow(FlowCommand("input_fill"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 900}),
    )
    context = FlowContext(
        DeviceSession(
            device=BrowserDevice(BrowserDeviceConfig(BrowserEngine.CHROMIUM)),
            data={"browser_data": SimpleNamespace(fill_locator=Mock())},
        ),
        flow_values=values,
    )

    handle_input_fill(
        FlowCommand(
            "input_fill",
            params={
                "locator": "#name",
                "value": "Ada",
                "min_delay": {"kind": "input", "id": _DURATION_INPUT_ID},
                "max_delay": {"kind": "variable", "id": _DURATION_VARIABLE_ID},
                "delay_ms": {"kind": "input", "id": _DURATION_INPUT_ID},
            },
        ),
        context,
    )

    context.session.data["browser_data"].fill_locator.assert_called_once_with(
        {"platform": "browser", "strategy": "css", "value": "#name"},
        "Ada",
        min_delay=900,
        max_delay=750,
    )


def test_windows_geometry_timing_editor_exposes_typed_scalar_pickers() -> None:
    for command_id, params, expected_types in (
        (
            "win_click",
            {
                "x": 10,
                "y": 20,
                "mode": "input",
                "focus_delay_ms": 100,
                "click_duration_ms": 150,
                "restore_delay_ms": 200,
            },
            ["integer", "integer", "duration_ms", "duration_ms", "duration_ms"],
        ),
        (
            "win_drag",
            {
                "x": 10,
                "y": 20,
                "xoffset": 30,
                "yoffset": 40,
                "mode": "input",
                "drag_duration_ms": 300,
                "drag_steps": 5,
                "focus_delay_ms": 100,
                "restore_delay_ms": 200,
            },
            [
                "integer",
                "integer",
                "integer",
                "integer",
                "duration_ms",
                "integer",
                "duration_ms",
                "duration_ms",
            ],
        ),
    ):
        command = FlowCommand(command_id, params=params)
        flow = _flow(command)
        card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))
        pickers = _configured_value_reference_pickers(card)

        assert [picker.currentData()[1] for picker in pickers] == expected_types
        card.deleteLater()


def test_win_scroll_editor_exposes_integer_reference_pickers() -> None:
    command = FlowCommand("win_scroll", params={"x": 10, "y": 20, "amount": -3})
    flow = _flow(command)
    card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))

    pickers = _configured_value_reference_pickers(card)

    assert [picker.currentData()[1] for picker in pickers] == [
        "integer",
        "integer",
        "integer",
    ]
    card.deleteLater()


def test_visual_target_editors_expose_typed_tuning_pickers() -> None:
    for command_id, params, expected_types in (
        (
            "click_best_target",
            {
                "source_x": 10.0,
                "source_y": 20.0,
                "source_mode": "fixed_position",
                "scoring_mode": "closest_to_bonus_path",
                "path_tolerance_px": 30.0,
                "preview_only": False,
                "log_timing": False,
                "hold_duration_ms": 100,
            },
            ["number", "number", "number", "boolean", "boolean", "duration_ms"],
        ),
        (
            "click_dynamic_target",
            {
                "target_source": "images",
                "strategy": "latest",
            },
            [
                "number",
                "boolean",
                "number",
                "number",
                "number",
                "number",
                "number",
                "number",
                "number",
                "number",
                "boolean",
                "duration_ms",
                "duration_ms",
                "integer",
                "integer",
                "number",
                "integer",
                "duration_ms",
                "number",
                "number",
                "number",
            ],
        ),
    ):
        command = FlowCommand(command_id, params=params)
        flow = _flow(command)
        card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))

        assert sorted(
            picker.currentData()[1]
            for picker in _configured_value_reference_pickers(card)
        ) == sorted(expected_types)
        card.deleteLater()


def test_visual_target_source_point_resolves_number_reference() -> None:
    flow = _flow(FlowCommand("click_best_target"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {}),
    )
    values.variables.set(_NUMBER_VARIABLE_ID, 123.0)
    context = FlowContext(
        DeviceSession(
            device=WindowsDevice(hwnd=10, title="Game"),
            data={"windows_data": {"hwnd": 10, "title": "Game"}},
        ),
        flow_values=values,
    )

    point = _source_point(
        FlowCommand(
            "click_best_target",
            params={
                "source_mode": "fixed_position",
                "source_x": {"kind": "variable", "id": _NUMBER_VARIABLE_ID},
                "source_y": {"kind": "variable", "id": _NUMBER_VARIABLE_ID},
            },
        ),
        context,
        SimpleNamespace(shape=(100, 200, 3)),
    )

    assert point == (123, 123)


def test_dynamic_target_runtime_resolves_typed_tuning_values() -> None:
    from automadin.services.dynamic_target import TargetObservation

    flow = _flow(FlowCommand("click_dynamic_target"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {}),
    )
    values.variables.set(_INTEGER_VARIABLE_ID, 2)
    values.variables.set(_NUMBER_VARIABLE_ID, 20.0)
    context = FlowContext(
        DeviceSession(
            device=WindowsDevice(hwnd=10, title="Game"),
            data={"windows_data": {"hwnd": 10, "title": "Game"}},
        ),
        flow_values=values,
    )
    context._executor = SimpleNamespace(_db=SimpleNamespace())
    centers = iter((10, 20))

    def candidates(_command, _context, _screenshot, timestamp):
        center = next(centers)
        return [TargetObservation(timestamp, center - 5, 5, 10, 10, 0.9)], (100, 100)

    with (
        patch(
            "automadin.execution.handlers.visual._take_screenshot_for_device",
            return_value=b"frame",
        ) as capture,
        patch(
            "automadin.execution.handlers.visual._dynamic_candidates",
            side_effect=candidates,
        ),
        patch("automadin.execution.handlers.visual._perform_click_at") as click,
    ):
        handle_click_dynamic_target(
            FlowCommand(
                "click_dynamic_target",
                params={
                    "strategy": "latest",
                    "confirmation_frames": {
                        "kind": "variable",
                        "id": _INTEGER_VARIABLE_ID,
                    },
                    "max_movement_px": {
                        "kind": "variable",
                        "id": _NUMBER_VARIABLE_ID,
                    },
                    "frame_interval_ms": 20,
                    "timeout_ms": 1000,
                    "preview_only": {
                        "kind": "variable",
                        "id": _BOOLEAN_VARIABLE_ID,
                    },
                },
            ),
            context,
        )

    assert capture.call_count == 2
    click.assert_not_called()
    assert context.get_variable("_dynamic_target_x") == "20"
    assert context.get_variable("_dynamic_target_frames") == "2"


def test_win_scroll_runtime_resolves_integer_references() -> None:
    flow = _flow(FlowCommand("win_scroll"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {}),
    )
    values.variables.set(_INTEGER_VARIABLE_ID, -3)
    context = FlowContext(
        DeviceSession(
            device=WindowsDevice(hwnd=10, title="Game"),
            data={"windows_data": {"hwnd": 10, "title": "Game"}},
        ),
        flow_values=values,
    )
    rect = SimpleNamespace(left=100, top=200, right=900, bottom=800)

    with (
        patch("automadin.execution.handlers.windows.sys.platform", "win32"),
        patch(
            "automadin.execution.handlers.windows.WindowsDeviceEngine.resolve_session_data",
            return_value={"hwnd": 10, "title": "Game"},
        ),
        patch("automadin.execution.handlers.windows._window_rect", return_value=rect),
        patch("automadin.execution.handlers.windows.scroll_window") as scroll,
    ):
        handle_win_scroll(
            FlowCommand(
                "win_scroll",
                params={
                    "x": {"kind": "variable", "id": _INTEGER_VARIABLE_ID},
                    "y": {"kind": "variable", "id": _INTEGER_VARIABLE_ID},
                    "amount": {"kind": "variable", "id": _INTEGER_VARIABLE_ID},
                },
            ),
            context,
        )

    scroll.assert_called_once_with(
        10,
        -3,
        -3,
        -3,
        delivery_mode="message",
        focus_delay_ms=200,
        restore_delay_ms=100,
    )


def test_win_click_runtime_resolves_duration_references() -> None:
    flow = _flow(FlowCommand("win_click"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 900}),
    )
    context = FlowContext(
        DeviceSession(
            device=WindowsDevice(hwnd=10, title="Game"),
            data={"windows_data": {"hwnd": 10, "title": "Game"}},
        ),
        flow_values=values,
    )

    with (
        patch("automadin.execution.handlers.windows.sys.platform", "win32"),
        patch(
            "automadin.execution.handlers.windows.WindowsDeviceEngine.resolve_session_data",
            return_value={"hwnd": 10, "title": "Game"},
        ),
        patch("automadin.execution.handlers.windows.click_window") as click_window,
    ):
        handle_win_click(
            FlowCommand(
                "win_click",
                params={
                    "x": 10,
                    "y": 20,
                    "focus_delay_ms": {"kind": "input", "id": _DURATION_INPUT_ID},
                    "click_duration_ms": {"kind": "variable", "id": _DURATION_VARIABLE_ID},
                    "restore_delay_ms": {"kind": "input", "id": _DURATION_INPUT_ID},
                },
            ),
            context,
        )

    click_window.assert_called_once_with(
        10,
        10,
        20,
        button="left",
        click_type="single",
        mode="message",
        focus_delay_ms=900,
        click_duration_ms=750,
        restore_delay_ms=900,
    )


def test_win_drag_runtime_resolves_duration_and_integer_references() -> None:
    flow = _flow(FlowCommand("win_drag"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 900}),
    )
    context = FlowContext(
        DeviceSession(
            device=WindowsDevice(hwnd=10, title="Game"),
            data={"windows_data": {"hwnd": 10, "title": "Game"}},
        ),
        flow_values=values,
    )

    with (
        patch("automadin.execution.handlers.windows.sys.platform", "win32"),
        patch(
            "automadin.execution.handlers.windows.WindowsDeviceEngine.resolve_session_data",
            return_value={"hwnd": 10, "title": "Game"},
        ),
        patch("automadin.execution.handlers.windows.drag_window") as drag_window,
    ):
        handle_win_drag(
            FlowCommand(
                "win_drag",
                params={
                    "x": 10,
                    "y": 20,
                    "xoffset": 30,
                    "yoffset": 40,
                    "drag_duration_ms": {"kind": "input", "id": _DURATION_INPUT_ID},
                    "drag_steps": {"kind": "variable", "id": _INTEGER_VARIABLE_ID},
                    "focus_delay_ms": {"kind": "variable", "id": _DURATION_VARIABLE_ID},
                    "restore_delay_ms": {"kind": "input", "id": _DURATION_INPUT_ID},
                },
            ),
            context,
        )

    drag_window.assert_called_once_with(
        10,
        10,
        20,
        40,
        60,
        button="left",
        mode="input",
        drag_duration_ms=900,
        steps=1,
        focus_delay_ms=750,
        restore_delay_ms=900,
    )


def test_coordinate_editors_expose_numeric_reference_pickers() -> None:
    for command_id, params, expected_types in (
        (
            "click_position",
            {"x": 10.0, "y": 20.0},
            ["number", "number"],
        ),
        (
            "swipe",
            {
                "start_x": 10.0,
                "start_y": 20.0,
                "end_x": 30.0,
                "end_y": 40.0,
                "duration_ms": 300,
            },
            ["number", "number", "number", "number", "duration_ms"],
        ),
    ):
        command = FlowCommand(command_id, params=params)
        flow = _flow(command)
        card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))
        pickers = _configured_value_reference_pickers(card)

        assert [picker.currentData()[1] for picker in pickers] == expected_types
        card.deleteLater()


def test_coordinate_resolver_resolves_number_and_integer_references() -> None:
    flow = _flow(FlowCommand("click_position"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)

    number_point = resolve_point(
        FlowCommand(
            "click_position",
            params={
                "coordinate_unit": "percent",
                "x": {"kind": "variable", "id": _NUMBER_VARIABLE_ID},
                "y": 50.0,
            },
        ),
        context,
        "x",
        "y",
        default_unit="pixels",
        surface_size=(1000, 800),
        value_type=ValueType.NUMBER,
    )
    integer_point = resolve_point(
        FlowCommand(
            "win_click",
            params={
                "coordinate_unit": "pixels",
                "x": {"kind": "variable", "id": _INTEGER_VARIABLE_ID},
                "y": 20,
            },
        ),
        context,
        "x",
        "y",
        default_unit="pixels",
        value_type=ValueType.INTEGER,
    )

    assert number_point == (9, 400)
    assert integer_point == (1, 20)


def test_loop_and_screenshot_editors_expose_typed_scalar_pickers() -> None:
    for command_id, params, expected_type in (
        ("for", {"count": 1}, "integer"),
        ("screenshot", {"save_to_disk": True}, "boolean"),
    ):
        command = FlowCommand(command_id, params=params)
        flow = _flow(command)
        card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))
        pickers = card.findChildren(QComboBox, "valueReferencePicker")

        assert any(picker.currentData() == ("literal", expected_type) for picker in pickers)
        card.deleteLater()


def test_for_runtime_resolves_integer_count_reference() -> None:
    flow = _flow(FlowCommand("for"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)
    executed: list[list[str]] = []
    context._executor = SimpleNamespace(
        _execute_children=lambda commands, _ctx: executed.append(
            [command.id for command in commands]
        )
    )

    handle_for(
        FlowCommand(
            "for",
            params={"count": {"kind": "variable", "id": _INTEGER_VARIABLE_ID}},
            children=[FlowCommand("body")],
        ),
        context,
    )

    assert executed == [["body"]]


def test_screenshot_runtime_resolves_boolean_save_reference(tmp_path) -> None:
    flow = _flow(FlowCommand("screenshot"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)

    with patch(
        "automadin.execution.handlers.detection._take_screenshot_for_device",
        return_value=b"png",
    ):
        handle_screenshot(
            FlowCommand(
                "screenshot",
                params={
                    "save_to_disk": {"kind": "variable", "id": _BOOLEAN_VARIABLE_ID},
                    "save_path": str(tmp_path),
                },
            ),
            context,
        )

    assert list(tmp_path.glob("*.png"))


def test_random_wait_and_android_install_editors_expose_typed_scalar_pickers() -> None:
    for command_id, params, expected_types in (
        (
            "random_wait",
            {"min_ms": 100, "max_ms": 200},
            ["duration_ms", "duration_ms"],
        ),
        (
            "android_install_apk",
            {
                "apk_path": "app.apk",
                "replace_existing": True,
                "grant_permissions": False,
                "timeout": 120000,
            },
            ["boolean", "boolean", "duration_ms"],
        ),
    ):
        command = FlowCommand(command_id, params=params)
        flow = _flow(command)
        card = CommandCard(command, load_command_types(), SimpleNamespace(_flow=flow))
        pickers = card.findChildren(QComboBox, "valueReferencePicker")

        assert [picker.currentData()[1] for picker in pickers if picker.currentData()] == expected_types
        card.deleteLater()


def test_random_wait_runtime_resolves_duration_bound_references() -> None:
    flow = _flow(FlowCommand("random_wait"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 900}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)

    with (
        patch("automadin.execution.handlers.wait.random.randint", return_value=500) as randint,
        patch.object(context, "interruptible_sleep") as sleep,
    ):
        handle_random_wait(
            FlowCommand(
                "random_wait",
                params={
                    "min_ms": {"kind": "input", "id": _DURATION_INPUT_ID},
                    "max_ms": {"kind": "variable", "id": _DURATION_VARIABLE_ID},
                },
            ),
            context,
        )

    randint.assert_called_once_with(900, 750)
    sleep.assert_called_once_with(0.5)


def test_android_install_runtime_resolves_boolean_references() -> None:
    flow = _flow(FlowCommand("android_install_apk"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {}),
    )
    context = FlowContext(
        DeviceSession(
            device=AndroidDevice(device_id="emulator-5554", model="Test"),
            data={"adb_data": {"adb_path": "selected-adb.exe", "device_id": "emulator-5554"}},
        ),
        flow_values=values,
    )

    with patch(
        "automadin.engine.device_controller.AdbDeviceEngine.install_apk",
        return_value="ok",
    ) as install:
        handle_android_install_apk(
            FlowCommand(
                "android_install_apk",
                params={
                    "apk_path": "app.apk",
                    "replace_existing": {"kind": "variable", "id": _BOOLEAN_VARIABLE_ID},
                    "grant_permissions": False,
                    "timeout": 1000,
                },
            ),
            context,
        )

    install.assert_called_once_with(
        context.session.data["adb_data"],
        "app.apk",
        replace_existing=True,
        grant_permissions=False,
        timeout=1,
    )


def test_press_keycode_runtime_resolves_duration_pause_reference() -> None:
    flow = _flow(FlowCommand("press_keycode"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 1250}),
    )
    context = FlowContext(
        DeviceSession(
            device=AndroidDevice(device_id="emulator-5554", model="Test"),
            data={"adb_data": {"adb_path": "selected-adb.exe", "device_id": "emulator-5554"}},
        ),
        flow_values=values,
    )

    with (
        patch("automadin.engine.device_controller.AndroidDeviceController._shell"),
        patch("automadin.engine.device_controller.time.sleep") as sleep,
    ):
        handle_press_keycode(
            FlowCommand(
                "press_keycode",
                params={
                    "keys": ["A", "B"],
                    "mode": "sequential",
                    "pause_ms": {"kind": "input", "id": _DURATION_INPUT_ID},
                },
            ),
            context,
        )

    assert sleep.call_args_list == [call(1.25), call(1.25)]


def test_win_key_runtime_resolves_duration_pause_reference() -> None:
    flow = _flow(FlowCommand("win_key"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 750}),
    )
    context = FlowContext(
        DeviceSession(
            device=WindowsDevice(hwnd=10, title="Game"),
            data={"windows_data": {"hwnd": 10, "title": "Game"}},
        ),
        flow_values=values,
    )
    with (
        patch("automadin.execution.handlers.windows.sys.platform", "win32"),
        patch(
            "automadin.execution.handlers.windows.WindowsDeviceEngine.resolve_session_data",
            return_value={"hwnd": 10, "title": "Game"},
        ),
        patch("automadin.execution.handlers.windows.press_window_keys") as press_keys,
    ):
        handle_win_key(
            FlowCommand(
                "win_key",
                params={
                    "keys": ["A", "B"],
                    "mode": "sequential",
                    "pause_ms": {"kind": "input", "id": _DURATION_INPUT_ID},
                },
            ),
            context,
        )

    assert press_keys.call_args.kwargs["pause_ms"] == 750


def test_type_text_runtime_resolves_duration_delay_reference() -> None:
    flow = _flow(FlowCommand("type_text"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 900}),
    )
    port = SimpleNamespace(type_text=Mock())
    context = FlowContext(
        DeviceSession(
            device=BrowserDevice(BrowserDeviceConfig(BrowserEngine.CHROMIUM)),
            data={"browser_data": port},
        ),
        flow_values=values,
    )

    handle_type_text(
        FlowCommand(
            "type_text",
            params={
                "text": "hello",
                "delay_ms": {"kind": "input", "id": _DURATION_INPUT_ID},
            },
        ),
        context,
    )

    port.type_text.assert_called_once_with(
        "hello", min_delay_ms=900, max_delay_ms=900
    )


def test_win_type_runtime_resolves_duration_delay_reference() -> None:
    flow = _flow(FlowCommand("win_type"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 700}),
    )
    context = FlowContext(
        DeviceSession(
            device=WindowsDevice(hwnd=10, title="Game"),
            data={"windows_data": {"hwnd": 10, "title": "Game"}},
        ),
        flow_values=values,
    )

    with (
        patch("automadin.execution.handlers.windows._require_windows"),
        patch(
            "automadin.execution.handlers.windows._get_win_data",
            return_value={"hwnd": 10},
        ),
        patch("automadin.execution.handlers.windows.type_window_text") as type_text,
    ):
        handle_win_type(
            FlowCommand(
                "win_type",
                params={
                    "text": "a",
                    "delay_ms": {"kind": "input", "id": _DURATION_INPUT_ID},
                },
            ),
            context,
        )

    assert type_text.call_args.kwargs["min_delay_ms"] == 700
    assert type_text.call_args.kwargs["max_delay_ms"] == 700


def test_type_text_runtime_resolves_duration_delay_range_references() -> None:
    flow = _flow(FlowCommand("type_text"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 900}),
    )
    port = SimpleNamespace(type_text=Mock())
    context = FlowContext(
        DeviceSession(
            device=BrowserDevice(BrowserDeviceConfig(BrowserEngine.CHROMIUM)),
            data={"browser_data": port},
        ),
        flow_values=values,
    )

    handle_type_text(
        FlowCommand(
            "type_text",
            params={
                "text": "hello",
                "min_delay": {"kind": "input", "id": _DURATION_INPUT_ID},
                "max_delay": {"kind": "variable", "id": _DURATION_VARIABLE_ID},
            },
        ),
        context,
    )

    port.type_text.assert_called_once_with(
        "hello", min_delay_ms=900, max_delay_ms=750
    )


def test_win_type_runtime_resolves_duration_delay_range_references() -> None:
    flow = _flow(FlowCommand("win_type"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 500}),
    )
    context = FlowContext(
        DeviceSession(
            device=WindowsDevice(hwnd=10, title="Game"),
            data={"windows_data": {"hwnd": 10, "title": "Game"}},
        ),
        flow_values=values,
    )

    with (
        patch("automadin.execution.handlers.windows._require_windows"),
        patch(
            "automadin.execution.handlers.windows._get_win_data",
            return_value={"hwnd": 10},
        ),
        patch("automadin.execution.handlers.windows.type_window_text") as type_text,
    ):
        handle_win_type(
            FlowCommand(
                "win_type",
                params={
                    "text": "a",
                    "min_delay": {"kind": "input", "id": _DURATION_INPUT_ID},
                    "max_delay": {"kind": "variable", "id": _DURATION_VARIABLE_ID},
                },
            ),
            context,
        )

    assert type_text.call_args.kwargs["min_delay_ms"] == 500
    assert type_text.call_args.kwargs["max_delay_ms"] == 750


def test_if_elapsed_runtime_resolves_duration_min_wait_reference() -> None:
    flow = _flow(FlowCommand("if_elapsed"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 900}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)
    context._executor = SimpleNamespace(
        _execute_children=lambda commands, _ctx: None,
    )

    with patch.object(context, "elapsed_block_remaining_ms", return_value=0) as remaining:
        handle_if_elapsed(
            FlowCommand(
                "if_elapsed",
                params={"min_wait_time": {"kind": "input", "id": _DURATION_INPUT_ID}},
                children=[FlowCommand("then")],
            ),
            context,
        )

    assert remaining.call_args.args[1] == 900


def test_executor_resolves_loop_min_wait_references() -> None:
    flow = _flow(FlowCommand("for"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 900}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)
    executor = FlowExecutor()
    executed: list[str] = []
    executor.register_handler("for", lambda _cmd, _ctx: executed.append("for"))

    with patch.object(context, "should_skip_min_wait", return_value=False) as skip:
        executor._execute_commands(
            [
                FlowCommand(
                    "for",
                    params={
                        "min_wait_time": {
                            "kind": "input",
                            "id": _DURATION_INPUT_ID,
                        }
                    },
                )
            ],
            context,
        )

    assert executed == ["for"]
    assert skip.call_args.args[1] == 900


def test_executor_resolves_wait_pause_reference() -> None:
    flow = _flow(FlowCommand("wait"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 900}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)
    executor = FlowExecutor()
    executor.register_handler("wait", lambda _cmd, _ctx: None)

    with patch("automadin.execution.executor._interruptible_sleep") as sleep:
        executor._execute_commands(
            [
                FlowCommand(
                    "wait",
                    params={
                        "pause_ms": {"kind": "input", "id": _DURATION_INPUT_ID}
                    },
                )
            ],
            context,
        )

    sleep.assert_called_once_with(0.9, context)


def test_app_lifecycle_editors_expose_duration_timeout_picker() -> None:
    _app()
    cases = (
        ("android_install_apk", {"apk_path": "app.apk", "timeout": 120000}, 3),
        (
            "android_launch_app",
            {"package_name": "com.example.app", "timeout": 15000},
            1,
        ),
        (
            "android_restart_app",
            {"package_name": "com.example.app", "timeout": 15000},
            1,
        ),
        (
            "android_start_activity",
            {
                "package_name": "com.example.app",
                "activity_name": ".MainActivity",
                "timeout": 15000,
            },
            1,
        ),
        (
            "android_open_deep_link",
            {"uri": "myapp://home", "timeout": 15000},
            1,
        ),
        ("win_ensure_app", {"timeout": 30000, "focus": False}, 2),
    )
    for command_id, params, expected_count in cases:
        command = FlowCommand(command_id, params=params)
        card = CommandCard(
            command,
            load_command_types(),
            SimpleNamespace(_flow=_flow(command)),
        )
        try:
            pickers = card.findChildren(QComboBox, "valueReferencePicker")
            assert len(pickers) == expected_count, command_id
            assert any(
                picker.currentData() == ("literal", "duration_ms")
                for picker in pickers
            ), command_id
        finally:
            card.deleteLater()


def test_value_reference_editor_normalizes_legacy_duration_literal() -> None:
    _app()
    changes: list[tuple[str, object]] = []
    widget = ParamEditorWidget(
        {
            "name": "ms",
            "type": "value_reference",
            "allowed_value_types": ["duration_ms"],
        },
        500,
        lambda name, value: changes.append((name, value)),
    )
    picker = widget.findChild(QComboBox, "valueReferencePicker")

    assert picker is not None
    assert picker.currentData() == ("literal", "duration_ms")
    assert changes == []


def test_preflight_checks_scalar_reference_type_and_stale_ids() -> None:
    valid = _flow(
        FlowCommand(
            "wait",
            params={
                "ms": {"kind": "variable", "id": _DURATION_VARIABLE_ID},
                "pause_ms": {"kind": "input", "id": _DURATION_INPUT_ID},
                "min_wait_time": {
                    "kind": "variable",
                    "id": _DURATION_VARIABLE_ID,
                },
            },
        )
    )
    assert not preflight_flow(valid).issues

    wrong_type = _flow(
        FlowCommand(
            "wait",
            params={"ms": {"kind": "variable", "id": _INTEGER_VARIABLE_ID}},
        )
    )
    assert {issue.code for issue in preflight_flow(wrong_type).issues} == {
        "VALUE_REFERENCE_TYPE_MISMATCH"
    }

    stale = _flow(
        FlowCommand(
            "wait",
            params={
                "ms": {
                    "kind": "variable",
                    "id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
                }
            },
        )
    )
    assert {issue.code for issue in preflight_flow(stale).issues} == {
        "INVALID_VALUE_REFERENCE"
    }


def test_preflight_limits_if_image_coordinates_to_then_descendants() -> None:
    match_x = {"kind": "builtin", "name": "_match_x"}
    match_y = {"kind": "builtin", "name": "_match_y"}
    nested_swipe = FlowCommand(
        "swipe",
        params={
            "coordinate_unit": "pixels",
            "start_x": match_x,
            "start_y": match_y,
            "end_x": match_x,
            "end_y": match_y,
        },
    )
    valid = _flow(
        FlowCommand(
            "if_image",
            params={"images": ["target"]},
            children=[nested_swipe],
        )
    )

    assert not {
        issue.code for issue in preflight_flow(valid).issues
    }.intersection({"INVALID_VALUE_REFERENCE", "VALUE_REFERENCE_TYPE_MISMATCH"})

    outside = _flow(nested_swipe)
    assert "INVALID_VALUE_REFERENCE" in {
        issue.code for issue in preflight_flow(outside).issues
    }


def test_wait_runtime_resolves_duration_variable_reference() -> None:
    flow = _flow(FlowCommand("wait"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 1000}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)
    captured: list[float] = []
    with patch.object(
        FlowContext,
        "interruptible_sleep",
        lambda _self, seconds: captured.append(seconds),
    ):
        handle_wait(
            FlowCommand(
                "wait",
                params={
                    "ms": {"kind": "variable", "id": _DURATION_VARIABLE_ID}
                },
            ),
            context,
        )

    assert captured == [0.75]


def test_wait_for_selector_runtime_resolves_duration_input_reference() -> None:
    flow = _flow(FlowCommand("wait_for_selector"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 1250}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)
    calls: list[tuple[object, int]] = []
    context.session.data["browser_data"] = SimpleNamespace(
        wait_for_locator=lambda locator, *, timeout_ms: calls.append(
            (locator, timeout_ms)
        )
    )

    handle_wait_for_selector(
        FlowCommand(
            "wait_for_selector",
            params={
                "locator": "#ready",
                "timeout": {"kind": "input", "id": _DURATION_INPUT_ID},
            },
        ),
        context,
    )

    assert calls == [
        (
            {"platform": "browser", "strategy": "css", "value": "#ready"},
                500,
        )
    ]


def test_wait_for_navigation_runtime_resolves_duration_variable_reference() -> None:
    flow = _flow(FlowCommand("wait_for_navigation"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 1000}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)
    calls: list[tuple[str, int]] = []
    context.session.data["browser_data"] = SimpleNamespace(
        active_page=SimpleNamespace(
            wait_for_load_state=lambda state, *, timeout: calls.append(
                (state, timeout)
            )
        )
    )

    handle_wait_for_navigation(
        FlowCommand(
            "wait_for_navigation",
            params={
                "timeout": {"kind": "variable", "id": _DURATION_VARIABLE_ID}
            },
        ),
        context,
    )

    assert calls == [("networkidle", 500)]


def test_click_element_runtime_resolves_duration_input_reference() -> None:
    flow = _flow(FlowCommand("click_element"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 1250}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)
    calls: list[tuple[object, int]] = []
    context.session.data["browser_data"] = SimpleNamespace(
        click_locator=lambda locator, *, timeout_ms: calls.append(
            (locator, timeout_ms)
        )
    )

    handle_click_element(
        FlowCommand(
            "click_element",
            params={
                "locator": "#save",
                "timeout": {"kind": "input", "id": _DURATION_INPUT_ID},
            },
        ),
        context,
    )

    assert calls == [
        (
            {"platform": "browser", "strategy": "css", "value": "#save"},
            1250,
        )
    ]


def test_if_image_runtime_resolves_duration_variable_reference() -> None:
    flow = _flow(FlowCommand("if_image"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 1000}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)
    executed: list[list[str]] = []
    context._executor = SimpleNamespace(
        _execute_children=lambda commands, _ctx: executed.append(
            [command.id for command in commands]
        )
    )

    with patch(
        "automadin.execution.handlers.control._check_image_condition",
        return_value=True,
    ) as check:
        handle_if_image(
            FlowCommand(
                "if_image",
                params={
                    "images": ["target"],
                    "timeout": {"kind": "variable", "id": _DURATION_VARIABLE_ID},
                },
                children=[FlowCommand("then")],
            ),
            context,
        )

    check.assert_called_once()
    assert executed == [["then"]]


def test_if_image_runtime_resolves_number_variable_reference() -> None:
    flow = _flow(FlowCommand("if_image"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 1000}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)
    context._executor = SimpleNamespace(
        _execute_children=lambda _commands, _ctx: None,
    )

    with patch(
        "automadin.execution.handlers.control._check_image_condition",
        return_value=True,
    ) as check:
        handle_if_image(
            FlowCommand(
                "if_image",
                params={
                    "images": ["target"],
                    "threshold": {"kind": "variable", "id": _NUMBER_VARIABLE_ID},
                },
                children=[FlowCommand("then")],
            ),
            context,
        )

    assert check.call_args.args[3] == 0.9


def test_if_image_runtime_resolves_color_tolerance_reference() -> None:
    flow = _flow(FlowCommand("if_image"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 1000}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)
    context._executor = SimpleNamespace(
        _execute_children=lambda _commands, _ctx: None,
    )

    with patch(
        "automadin.execution.handlers.control._check_image_condition",
        return_value=True,
    ) as check:
        handle_if_image(
            FlowCommand(
                "if_image",
                params={
                    "images": ["target"],
                    "color_match": {
                        "kind": "variable",
                        "id": _BOOLEAN_VARIABLE_ID,
                    },
                    "color_tolerance": {
                        "kind": "variable",
                        "id": _NUMBER_VARIABLE_ID,
                    },
                },
                children=[FlowCommand("then")],
            ),
            context,
        )

    assert check.call_args.args[4] is True
    assert check.call_args.args[5] == 0.9


def test_while_runtime_resolves_duration_input_reference() -> None:
    flow = _flow(FlowCommand("while"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 0}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)
    executed: list[list[str]] = []
    context._executor = SimpleNamespace(
        _execute_children=lambda commands, _ctx: executed.append(
            [command.id for command in commands]
        )
    )

    with patch(
        "automadin.execution.handlers.control._check_image_condition",
        return_value=True,
    ) as check:
        handle_while(
            FlowCommand(
                "while",
                params={
                    "images": ["target"],
                    "timeout": {"kind": "input", "id": _DURATION_INPUT_ID},
                },
                children=[FlowCommand("body")],
            ),
            context,
        )

    check.assert_not_called()
    assert executed == []


def test_while_runtime_resolves_number_variable_reference() -> None:
    flow = _flow(FlowCommand("while"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 0}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)
    context._executor = SimpleNamespace(
        _execute_children=lambda _commands, _ctx: None,
    )

    with patch(
        "automadin.execution.handlers.control._check_image_condition",
        return_value=False,
    ) as check:
        handle_while(
            FlowCommand(
                "while",
                params={
                    "images": ["target"],
                    "timeout": 1000,
                    "threshold": {"kind": "variable", "id": _NUMBER_VARIABLE_ID},
                },
                children=[FlowCommand("body")],
            ),
            context,
        )

    assert check.call_args.args[3] == 0.9


def test_while_runtime_resolves_color_tolerance_reference() -> None:
    flow = _flow(FlowCommand("while"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 1000}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)
    context._executor = SimpleNamespace(
        _execute_children=lambda _commands, _ctx: None,
    )

    with patch(
        "automadin.execution.handlers.control._check_image_condition",
        return_value=False,
    ) as check:
        handle_while(
            FlowCommand(
                "while",
                params={
                    "images": ["target"],
                    "timeout": 1000,
                    "color_match": {
                        "kind": "variable",
                        "id": _BOOLEAN_VARIABLE_ID,
                    },
                    "color_tolerance": {
                        "kind": "variable",
                        "id": _NUMBER_VARIABLE_ID,
                    },
                },
                children=[FlowCommand("body")],
            ),
            context,
        )

    assert check.call_args.args[4] is True
    assert check.call_args.args[5] == 0.9


def test_click_image_runtime_resolves_number_references() -> None:
    flow = _flow(FlowCommand("click_image"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 1000}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)
    match = SimpleNamespace(center_x=10, center_y=20)

    with (
        patch(
            "automadin.execution.handlers.actions._take_screenshot_for_device",
            return_value=b"png",
        ),
        patch(
            "automadin.execution.handlers.actions._find_any_image",
            return_value=("target", match),
        ) as find_image,
        patch("automadin.execution.handlers.actions._perform_click_at") as perform_click,
    ):
        handle_click_image(
            FlowCommand(
                "click_image",
                params={
                    "images": ["target"],
                    "threshold": {"kind": "variable", "id": _NUMBER_VARIABLE_ID},
                    "color_match": {
                        "kind": "variable",
                        "id": _BOOLEAN_VARIABLE_ID,
                    },
                    "color_tolerance": {
                        "kind": "variable",
                        "id": _NUMBER_VARIABLE_ID,
                    },
                    "xmargin": {"kind": "variable", "id": _INTEGER_VARIABLE_ID},
                    "ymargin": 2,
                    "hold_duration_ms": {"kind": "input", "id": _DURATION_INPUT_ID},
                },
            ),
            context,
        )

    assert find_image.call_args.args[3] == 0.9
    assert find_image.call_args.args[4] is True
    assert find_image.call_args.args[5] == 0.9
    perform_click.assert_called_once_with(
        context,
        11,
        22,
        click_type="single",
        hold_duration_ms=1000,
    )


def test_if_image_runtime_resolves_click_boolean_hold_and_offset_references() -> None:
    flow = _flow(FlowCommand("if_image"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 1000}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)
    context._executor = SimpleNamespace(_execute_children=lambda _commands, _ctx: None)

    with (
        patch("automadin.execution.handlers.control._check_image_condition", return_value=True),
        patch("automadin.execution.handlers.control._do_click_if_found") as click,
    ):
        handle_if_image(
            FlowCommand(
                "if_image",
                params={
                    "images": ["target"],
                    "clickIfFound": {"kind": "variable", "id": _BOOLEAN_VARIABLE_ID},
                    "hold_duration_ms": {"kind": "input", "id": _DURATION_INPUT_ID},
                    "xmargin": {"kind": "variable", "id": _INTEGER_VARIABLE_ID},
                    "ymargin": 2,
                },
            ),
            context,
        )

    click.assert_called_once_with(context, 1, 2, 1000)


def test_find_image_runtime_resolves_image_scalar_references() -> None:
    flow = _flow(FlowCommand("find_image"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 1000}),
    )
    context = FlowContext(DeviceSession(), flow_values=values)
    context._last_screenshot = b"png"
    match = SimpleNamespace(center_x=10, center_y=20, confidence=0.95)

    with patch(
        "automadin.execution.handlers.detection._find_any_image",
        return_value=("target", match),
    ) as find_image:
        handle_find_image(
            FlowCommand(
                "find_image",
                params={
                    "images": ["target"],
                    "threshold": {"kind": "variable", "id": _NUMBER_VARIABLE_ID},
                    "color_match": {
                        "kind": "variable",
                        "id": _BOOLEAN_VARIABLE_ID,
                    },
                    "color_tolerance": {
                        "kind": "variable",
                        "id": _NUMBER_VARIABLE_ID,
                    },
                },
            ),
            context,
        )

    assert find_image.call_args.args[3] == 0.9
    assert find_image.call_args.args[4] is True
    assert find_image.call_args.args[5] == 0.9


def test_win_if_image_runtime_resolves_duration_input_reference() -> None:
    flow = _flow(FlowCommand("win_if_image"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 20}),
    )
    context = FlowContext(
        DeviceSession(
            device=WindowsDevice(hwnd=10, title="Game"),
            data={"windows_data": {"hwnd": 10, "title": "Game"}},
        ),
        flow_values=values,
    )
    executed: list[list[str]] = []
    context._executor = SimpleNamespace(
        _execute_children=lambda commands, _ctx: executed.append(
            [command.id for command in commands]
        )
    )

    with (
        patch("automadin.execution.handlers.windows.sys.platform", "win32"),
        patch(
            "automadin.execution.handlers.windows.WindowsDeviceEngine.resolve_session_data",
            return_value={"hwnd": 10, "title": "Game"},
        ),
        patch(
            "automadin.execution.handlers.windows.time.monotonic",
            side_effect=[1.0, 1.020],
        ),
        patch(
            "automadin.execution.handlers.windows._take_screenshot_for_device",
            return_value=b"png",
        ),
        patch(
            "automadin.execution.handlers.windows._find_any_image",
            return_value=(None, None),
        ) as find_image,
    ):
        handle_win_if_image(
            FlowCommand(
                "win_if_image",
                params={
                    "images": ["target"],
                    "timeout": {"kind": "input", "id": _DURATION_INPUT_ID},
                    "color_match": {
                        "kind": "variable",
                        "id": _BOOLEAN_VARIABLE_ID,
                    },
                    "threshold": {"kind": "variable", "id": _NUMBER_VARIABLE_ID},
                    "color_tolerance": {
                        "kind": "variable",
                        "id": _NUMBER_VARIABLE_ID,
                    },
                },
                children=[FlowCommand("then")],
                else_children=[FlowCommand("else")],
            ),
            context,
        )

    assert executed == [["else"]]
    assert find_image.call_args.args[4] is True
    assert find_image.call_args.args[3] == 0.9


def test_win_click_image_runtime_resolves_duration_and_offset_references() -> None:
    flow = _flow(FlowCommand("win_click_image"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 900}),
    )
    context = FlowContext(
        DeviceSession(
            device=WindowsDevice(hwnd=10, title="Game"),
            data={"windows_data": {"hwnd": 10, "title": "Game"}},
        ),
        flow_values=values,
    )
    match = SimpleNamespace(center_x=10, center_y=20)

    with (
        patch("automadin.execution.handlers.windows.sys.platform", "win32"),
        patch(
            "automadin.execution.handlers.windows.WindowsDeviceEngine.resolve_session_data",
            return_value={"hwnd": 10, "title": "Game"},
        ),
        patch("automadin.execution.handlers.windows._take_screenshot_for_device", return_value=b"png"),
        patch("automadin.execution.handlers.windows._find_any_image", return_value=("target", match)),
        patch("automadin.execution.handlers.windows.click_window") as click_window,
    ):
        handle_win_click_image(
            FlowCommand(
                "win_click_image",
                params={
                    "images": ["target"],
                    "xmargin": {"kind": "variable", "id": _INTEGER_VARIABLE_ID},
                    "ymargin": 2,
                    "focus_delay_ms": {"kind": "input", "id": _DURATION_INPUT_ID},
                    "click_duration_ms": {"kind": "variable", "id": _DURATION_VARIABLE_ID},
                    "restore_delay_ms": {"kind": "input", "id": _DURATION_INPUT_ID},
                },
            ),
            context,
        )

    click_window.assert_called_once_with(
        10,
        11,
        22,
        button="left",
        click_type="single",
        mode="message",
        focus_delay_ms=900,
        click_duration_ms=750,
        restore_delay_ms=900,
    )


def test_win_while_runtime_resolves_duration_input_reference() -> None:
    flow = _flow(FlowCommand("win_while"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 1}),
    )
    context = FlowContext(
        DeviceSession(
            device=WindowsDevice(hwnd=10, title="Game"),
            data={"windows_data": {"hwnd": 10, "title": "Game"}},
        ),
        flow_values=values,
    )
    executed: list[list[str]] = []
    context._executor = SimpleNamespace(
        _execute_children=lambda commands, _ctx: executed.append(
            [command.id for command in commands]
        )
    )

    with (
        patch("automadin.execution.handlers.windows.sys.platform", "win32"),
        patch(
            "automadin.execution.handlers.windows.WindowsDeviceEngine.resolve_session_data",
            return_value={"hwnd": 10, "title": "Game"},
        ),
        patch(
            "automadin.execution.handlers.windows.time.monotonic",
            side_effect=[1.0, 1.01],
        ),
        patch(
            "automadin.execution.handlers.windows._take_screenshot_for_device"
        ) as screenshot,
    ):
        handle_win_while(
            FlowCommand(
                "win_while",
                params={
                    "images": ["target"],
                    "timeout": {"kind": "input", "id": _DURATION_INPUT_ID},
                },
                children=[FlowCommand("body")],
            ),
            context,
        )

    screenshot.assert_not_called()
    assert executed == []


def test_win_while_runtime_resolves_number_image_references() -> None:
    flow = _flow(FlowCommand("win_while"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 1000}),
    )
    context = FlowContext(
        DeviceSession(
            device=WindowsDevice(hwnd=10, title="Game"),
            data={"windows_data": {"hwnd": 10, "title": "Game"}},
        ),
        flow_values=values,
    )
    context._executor = SimpleNamespace(
        _execute_children=lambda _commands, _ctx: None,
    )

    with (
        patch("automadin.execution.handlers.windows.sys.platform", "win32"),
        patch(
            "automadin.execution.handlers.windows.WindowsDeviceEngine.resolve_session_data",
            return_value={"hwnd": 10, "title": "Game"},
        ),
        patch(
            "automadin.execution.handlers.windows.time.monotonic",
            side_effect=[1.0, 1.01],
        ),
        patch(
            "automadin.execution.handlers.windows._take_screenshot_for_device",
            return_value=b"png",
        ),
        patch(
            "automadin.execution.handlers.windows._find_any_image",
            return_value=(None, None),
        ) as find_image,
    ):
        handle_win_while(
            FlowCommand(
                "win_while",
                params={
                    "images": ["target"],
                    "timeout": 1000,
                    "color_match": {
                        "kind": "variable",
                        "id": _BOOLEAN_VARIABLE_ID,
                    },
                    "threshold": {"kind": "variable", "id": _NUMBER_VARIABLE_ID},
                    "color_tolerance": {
                        "kind": "variable",
                        "id": _NUMBER_VARIABLE_ID,
                    },
                },
                children=[FlowCommand("body")],
            ),
            context,
        )

    assert find_image.call_args.args[4] is True
    assert find_image.call_args.args[3] == 0.9
    assert find_image.call_args.args[5] == 0.9


def test_android_install_runtime_resolves_duration_input_reference() -> None:
    flow = _flow(FlowCommand("android_install_apk"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 1501}),
    )
    context = FlowContext(
        DeviceSession(
            device=AndroidDevice(device_id="emulator-5554", model="Test"),
            data={"adb_data": {"adb_path": "selected-adb.exe"}},
        ),
        flow_values=values,
    )

    with patch(
        "automadin.engine.device_controller.AdbDeviceEngine"
    ) as engine_type:
        handle_android_install_apk(
            FlowCommand(
                "android_install_apk",
                params={
                    "apk_path": "app.apk",
                    "timeout": {"kind": "input", "id": _DURATION_INPUT_ID},
                },
            ),
            context,
        )

    engine_type.assert_called_once_with(adb_path="selected-adb.exe")
    engine_type.return_value.install_apk.assert_called_once()
    assert engine_type.return_value.install_apk.call_args.kwargs["timeout"] == 2


def test_android_launch_runtime_resolves_duration_input_reference() -> None:
    flow = _flow(FlowCommand("android_launch_app"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 2501}),
    )
    context = FlowContext(
        DeviceSession(
            device=AndroidDevice(device_id="emulator-5554", model="Test"),
            data={"adb_data": {"adb_path": "selected-adb.exe"}},
        ),
        flow_values=values,
    )

    with patch(
        "automadin.engine.device_controller.AdbDeviceEngine"
    ) as engine_type:
        engine_type.return_value.ensure_package_foreground.return_value = True
        handle_android_launch_app(
            FlowCommand(
                "android_launch_app",
                params={
                    "package_name": "com.example.app",
                    "timeout": {"kind": "input", "id": _DURATION_INPUT_ID},
                },
            ),
            context,
        )

    engine_type.assert_called_once_with(adb_path="selected-adb.exe")
    engine_type.return_value.ensure_package_foreground.assert_called_once_with(
        {"adb_path": "selected-adb.exe"},
        "com.example.app",
        timeout=2.501,
    )


def test_android_restart_runtime_resolves_duration_input_reference() -> None:
    flow = _flow(FlowCommand("android_restart_app"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 3501}),
    )
    context = FlowContext(
        DeviceSession(
            device=AndroidDevice(device_id="emulator-5554", model="Test"),
            data={"adb_data": {"adb_path": "selected-adb.exe"}},
        ),
        flow_values=values,
    )

    with patch(
        "automadin.engine.device_controller.AdbDeviceEngine"
    ) as engine_type:
        handle_android_restart_app(
            FlowCommand(
                "android_restart_app",
                params={
                    "package_name": "com.example.app",
                    "timeout": {"kind": "input", "id": _DURATION_INPUT_ID},
                },
            ),
            context,
        )

    engine_type.assert_called_once_with(adb_path="selected-adb.exe")
    engine_type.return_value.restart_package.assert_called_once_with(
        {"adb_path": "selected-adb.exe"},
        "com.example.app",
        timeout=3.501,
    )


def test_android_start_activity_runtime_resolves_duration_input_reference() -> None:
    flow = _flow(FlowCommand("android_start_activity"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 4501}),
    )
    context = FlowContext(
        DeviceSession(
            device=AndroidDevice(device_id="emulator-5554", model="Test"),
            data={"adb_data": {"adb_path": "selected-adb.exe"}},
        ),
        flow_values=values,
    )

    with patch(
        "automadin.engine.device_controller.AdbDeviceEngine"
    ) as engine_type:
        handle_android_start_activity(
            FlowCommand(
                "android_start_activity",
                params={
                    "package_name": "com.example.app",
                    "activity_name": ".MainActivity",
                    "timeout": {"kind": "input", "id": _DURATION_INPUT_ID},
                },
            ),
            context,
        )

    engine_type.assert_called_once_with(adb_path="selected-adb.exe")
    engine_type.return_value.start_activity.assert_called_once_with(
        {"adb_path": "selected-adb.exe"},
        "com.example.app",
        ".MainActivity",
        timeout=4.501,
    )


def test_android_open_deep_link_runtime_resolves_duration_input_reference() -> None:
    flow = _flow(FlowCommand("android_open_deep_link"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 5501}),
    )
    context = FlowContext(
        DeviceSession(
            device=AndroidDevice(device_id="emulator-5554", model="Test"),
            data={"adb_data": {"adb_path": "selected-adb.exe"}},
        ),
        flow_values=values,
    )

    with patch(
        "automadin.engine.device_controller.AdbDeviceEngine"
    ) as engine_type:
        handle_android_open_deep_link(
            FlowCommand(
                "android_open_deep_link",
                params={
                    "uri": "myapp://home",
                    "package_name": "com.example.app",
                    "timeout": {"kind": "input", "id": _DURATION_INPUT_ID},
                },
            ),
            context,
        )

    engine_type.assert_called_once_with(adb_path="selected-adb.exe")
    engine_type.return_value.open_deep_link.assert_called_once_with(
        {"adb_path": "selected-adb.exe"},
        "myapp://home",
        package_name="com.example.app",
        timeout=5.501,
    )


def test_win_ensure_app_runtime_resolves_duration_input_reference() -> None:
    flow = _flow(FlowCommand("win_ensure_app"))
    values = FlowInvocationValues(
        flow.flow_id,
        flow.value_catalog,
        resolve_flow_inputs(flow.value_catalog.inputs, {_DURATION_INPUT_ID: 6501}),
    )
    session = DeviceSession(
        device=WindowsDevice(hwnd=10, title="Old", executable_path=r"C:\Apps\app.exe"),
        data={
            "windows_data": {
                "hwnd": 10,
                "title": "Old",
                "executable_path": r"C:\Apps\app.exe",
            }
        },
    )
    context = FlowContext(session, flow_values=values)

    with (
        patch("automadin.execution.handlers.windows._require_windows"),
        patch("automadin.execution.handlers.windows.WindowsDeviceEngine") as engine_type,
    ):
        handle_win_ensure_app(
            FlowCommand(
                "win_ensure_app",
                params={
                    "timeout": {"kind": "input", "id": _DURATION_INPUT_ID},
                    "focus": {"kind": "variable", "id": _BOOLEAN_VARIABLE_ID},
                },
            ),
            context,
        )

    engine_type.return_value.ensure_application.assert_called_once_with(
        session.data["windows_data"],
        executable_path="",
        arguments=[],
        window_title="",
        timeout=6.501,
    )
    engine_type.return_value.restore_and_focus.assert_called_once_with(
        session.data["windows_data"]
    )
