from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, QPoint
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QLabel,
    QToolButton,
    QWidget,
)

from automadin.execution.handlers.registry import (
    get_default_params,
    load_command_types,
)
from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    AutomationFlow,
    FlowCommand,
)
from automadin.models.hook import AutomationHook, VisualImageHookCondition
from automadin.ui.flow_editor.command_card import CommandCard
from automadin.ui.flow_editor.command_presentation import (
    COMMAND_PRESENTATIONS,
    IMAGE_SCOPE_SECTION,
    IMAGE_TARGET_SECTION,
    validate_command_presentations,
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _Editor:
    def __init__(self, flow: AutomationFlow) -> None:
        self._flow = flow
        self.section_states: dict[tuple[str, str], bool] = {}

    def _command_section_expanded(
        self,
        step_id: str,
        section_key: str,
        default: bool,
    ) -> bool:
        return self.section_states.get((step_id, section_key), default)

    def _set_command_section_expanded(
        self,
        step_id: str,
        section_key: str,
        expanded: bool,
    ) -> None:
        self.section_states[(step_id, section_key)] = expanded

    @staticmethod
    def _find_parent_command(
        _commands: list[FlowCommand],
        _target: FlowCommand,
    ) -> FlowCommand | None:
        return None


class _TopLevelShowRecorder(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.widgets: list[QWidget] = []

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.Show
            and isinstance(watched, QWidget)
            and watched.isWindow()
        ):
            self.widgets.append(watched)
        return False


def _card(command_id: str) -> tuple[CommandCard, FlowCommand, _Editor]:
    _app()
    command_types = load_command_types()
    command = FlowCommand(
        command_id,
        params=get_default_params(command_types[command_id]),
    )
    flow = AutomationFlow(
        "Presentation",
        [command],
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
    )
    editor = _Editor(flow)
    return CommandCard(command, command_types, editor), command, editor


def test_presentations_cover_every_runtime_command_and_parameter() -> None:
    command_types = load_command_types()

    validate_command_presentations(command_types)

    assert set(COMMAND_PRESENTATIONS) == set(command_types)


def test_every_runtime_command_instantiates_from_defaults() -> None:
    for command_id in load_command_types():
        card, _command, _editor = _card(command_id)
        assert card is not None
        card.deleteLater()


def test_group_card_offers_existing_visual_hook() -> None:
    _app()
    command_types = load_command_types()
    command = FlowCommand("group", params={"text": "Protected steps"})
    hook = AutomationHook(
        "Catch The Chest",
        VisualImageHookCondition(("chest.png",)),
    )
    flow = AutomationFlow("Hooks", [command], hooks=[hook])
    card = CommandCard(command, command_types, _Editor(flow))

    labels = {checkbox.text() for checkbox in card.findChildren(QCheckBox)}

    assert "Catch The Chest" in labels
    card.deleteLater()


def test_phase_e_covers_every_remaining_command_family() -> None:
    command_types = load_command_types()
    for category in (
        "ACTIONS",
        "ANDROID",
        "BROWSER",
        "CONTROL_FLOW",
        "DETECTION",
        "ORGANIZATION",
        "TIMING",
        "WINDOWS",
    ):
        category_ids = {
            command_id
            for command_id, definition in command_types.items()
            if definition.get("category") == category
        }
        assert category_ids <= set(COMMAND_PRESENTATIONS)


def test_screenshot_save_path_is_visible_only_when_saving() -> None:
    card, command, _editor = _card("screenshot")

    save_section = card.findChild(QWidget, "guidedSection_save")
    assert save_section is not None
    assert save_section.property("presentationVisible") is False

    command.params["save_path"] = "C:/captures"
    command.params["save_to_disk"] = True
    card._rebuild_params()
    save_section = card.findChild(QWidget, "guidedSection_save")
    assert save_section is not None
    assert save_section.property("presentationVisible") is True

    command.params["save_to_disk"] = False
    card._rebuild_params()
    assert command.params["save_path"] == "C:/captures"
    card.deleteLater()


def test_windows_point_input_hides_physical_timing_in_background_mode() -> None:
    for command_id, physical_fields in (
        (
            "win_click",
            ("focus_delay_ms", "click_duration_ms", "restore_delay_ms"),
        ),
        ("win_drag", ("focus_delay_ms", "restore_delay_ms")),
    ):
        card, command, _editor = _card(command_id)

        command.params["mode"] = "message"
        card._rebuild_params()
        for name in physical_fields:
            widget = card.findChild(QWidget, f"commandField_{name}")
            assert widget is not None
            assert widget.property("presentationVisible") is False

        command.params["mode"] = "input"
        card._rebuild_params()
        for name in physical_fields:
            widget = card.findChild(QWidget, f"commandField_{name}")
            assert widget is not None
            assert widget.property("presentationVisible") is True
        if command_id == "win_click":
            action_summary = card.findChild(
                QLabel, "guidedSectionSummary_action"
            )
            assert action_summary is not None
            assert "physical input" in action_summary.text()
        card.deleteLater()


def test_windows_background_delivery_hides_focus_timing() -> None:
    for command_id in ("win_key", "win_type", "win_scroll"):
        card, command, _editor = _card(command_id)

        command.params["delivery_mode"] = "message"
        card._rebuild_params()
        for name in ("focus_delay_ms", "restore_delay_ms"):
            widget = card.findChild(QWidget, f"commandField_{name}")
            assert widget is not None
            assert widget.property("presentationVisible") is False

        command.params["delivery_mode"] = "input"
        card._rebuild_params()
        for name in ("focus_delay_ms", "restore_delay_ms"):
            widget = card.findChild(QWidget, f"commandField_{name}")
            assert widget is not None
            assert widget.property("presentationVisible") is True
        card.deleteLater()


def test_windows_keyboard_delivery_choices_have_hover_guidance() -> None:
    expected_options = [
        "background_text",
        "background_key",
        "background_combined",
        "background_activated",
        "input",
    ]
    command_types = load_command_types()

    for command_id in ("win_key", "win_type"):
        definition = next(
            item
            for item in command_types[command_id]["params"]
            if item["name"] == "delivery_mode"
        )
        assert definition["label"] == "Delivery *"
        assert definition["options"] == expected_options
        assert "do not bring the app forward" in definition["description"]

        card, _command, _editor = _card(command_id)
        label = next(
            item
            for item in card.findChildren(QLabel)
            if item.text() == "Delivery *"
        )
        assert "experimental" in label.toolTip()
        card.deleteLater()


def test_legacy_keyboard_message_delivery_displays_its_new_equivalent() -> None:
    expected = {
        "win_key": "background_key",
        "win_type": "background_text",
    }
    for command_id, displayed_value in expected.items():
        card, command, _editor = _card(command_id)
        command.params["delivery_mode"] = "message"
        card._rebuild_params()

        field = card.findChild(QWidget, "commandField_delivery_mode")
        assert field is not None
        combo = field.findChild(QComboBox)
        assert combo is not None
        assert combo.currentData() == displayed_value
        assert command.params["delivery_mode"] == "message"
        card.deleteLater()


def test_phase_d_covers_value_comparisons_loops_updates_and_subflows() -> None:
    assert {
        "for",
        "for_each",
        "if_elapsed",
        "if_value",
        "run_flow",
        "switch_case",
        "switch_default",
        "switch_value",
        "update_variable",
        "while_value",
    } <= set(COMMAND_PRESENTATIONS)


def test_phase_d_uses_guided_layout_only_for_multi_concept_commands() -> None:
    guided = {
        "for_each",
        "if_value",
        "switch_value",
        "update_variable",
        "while_value",
    }
    compact = {
        "for",
        "if_elapsed",
        "run_flow",
        "switch_case",
        "switch_default",
    }

    assert all(COMMAND_PRESENTATIONS[item].layout == "guided" for item in guided)
    assert all(COMMAND_PRESENTATIONS[item].layout == "compact" for item in compact)


def test_value_condition_omits_irrelevant_dynamic_editors() -> None:
    card, command, _editor = _card("if_value")

    condition_summary = card.findChild(
        QLabel, "guidedSectionSummary_condition"
    )
    assert condition_summary is not None
    assert condition_summary.text() == "Typed comparison"

    command.params["operator"] = "is_empty"
    command.params["right"] = {"kind": "literal", "value": "retained"}
    card._rebuild_params()

    assert card.findChild(QWidget, "commandField_right") is None
    ignore_case = card.findChild(QWidget, "commandField_ignore_case")
    assert ignore_case is not None
    assert ignore_case.property("presentationVisible") is False
    card.deleteLater()


def test_update_variable_sections_follow_configured_operand_contract() -> None:
    card, command, _editor = _card("update_variable")

    def section_visible(key: str) -> bool:
        section = card.findChild(QWidget, f"guidedSection_{key}")
        assert section is not None
        return section.property("presentationVisible") is True

    assert section_visible("value")
    assert not section_visible("replace")
    assert card.findChild(QWidget, "commandField_source") is not None

    command.params["operation"] = "replace"
    card._rebuild_params()
    assert not section_visible("value")
    assert section_visible("replace")
    assert card.findChild(QWidget, "commandField_source") is None
    assert card.findChild(QWidget, "commandField_find") is not None
    assert card.findChild(QWidget, "commandField_replacement") is not None

    command.params["operation"] = "toggle"
    card._rebuild_params()
    assert not section_visible("value")
    assert not section_visible("replace")
    assert card.findChild(QWidget, "commandField_source") is None
    assert card.findChild(QWidget, "commandField_find") is None
    card.deleteLater()


def test_phase_c_covers_every_browser_command_and_browser_element_loop() -> None:
    command_types = load_command_types()
    browser_commands = {
        command_id
        for command_id, definition in command_types.items()
        if definition.get("category") == "BROWSER"
    }

    assert browser_commands <= set(COMMAND_PRESENTATIONS)
    assert "for_each_browser_element" in COMMAND_PRESENTATIONS


def test_browser_commands_use_compact_or_guided_layout_by_complexity() -> None:
    compact = {
        "assert_url_contains",
        "close_all_tabs",
        "close_tab",
        "ensure_browser_page",
        "go_back",
        "go_forward",
        "navigate_to",
        "open_new_tab",
        "reload_page",
        "switch_tab",
        "wait_for_navigation",
    }
    guided = {
        "assert_visible",
        "click_element",
        "evaluate_js",
        "extract_text",
        "for_each_browser_element",
        "input_fill",
        "select_browser_page",
        "wait_for_selector",
    }

    assert all(COMMAND_PRESENTATIONS[item].layout == "compact" for item in compact)
    assert all(COMMAND_PRESENTATIONS[item].layout == "guided" for item in guided)


def test_browser_page_selection_reveals_match_fields_only_for_text_rules() -> None:
    card, command, _editor = _card("select_browser_page")

    def visible(name: str) -> bool:
        widget = card.findChild(QWidget, f"commandField_{name}")
        assert widget is not None
        return widget.property("presentationVisible") is True

    assert not visible("value")
    assert not visible("match_case")

    command.params["strategy"] = "url_contains"
    card._rebuild_params()
    assert visible("value")
    assert visible("match_case")

    command.params["value"] = "example.test/rewards"
    command.params["match_case"] = True
    command.params["strategy"] = "newest_child"
    card._rebuild_params()
    assert not visible("value")
    assert not visible("match_case")
    assert command.params["value"] == "example.test/rewards"
    assert command.params["match_case"] is True
    card.deleteLater()


def test_browser_current_element_modes_hide_locators_without_erasing_them() -> None:
    cases = (
        ("click_element", "locator", None),
        ("extract_text", "locator", "child_selector"),
    )
    for command_id, locator_name, child_name in cases:
        card, command, _editor = _card(command_id)
        locator = {"kind": "css", "value": ".reward-card"}
        command.params[locator_name] = locator
        command.params["target"] = "current_element"
        if child_name is not None:
            command.params[child_name] = ".points"
        card._rebuild_params()

        locator_widget = card.findChild(QWidget, f"commandField_{locator_name}")
        assert locator_widget is not None
        assert locator_widget.property("presentationVisible") is False
        assert command.params[locator_name] == locator

        if child_name is not None:
            child_widget = card.findChild(QWidget, f"commandField_{child_name}")
            assert child_widget is not None
            assert child_widget.property("presentationVisible") is True
        card.deleteLater()


def test_visual_target_family_uses_approved_sections_and_defaults() -> None:
    expected_by_command = {
        "click_best_target": {
            "target": True,
            "selection": True,
            "action": True,
            "timing": False,
            "diagnostics": False,
            "failure": False,
        },
        "click_dynamic_target": {
            "target": True,
            "action": True,
            "scope": False,
            "timing": False,
            "advanced": False,
            "failure": False,
        },
    }
    for command_id, expected in expected_by_command.items():
        card, _command, _editor = _card(command_id)
        for key, expanded in expected.items():
            button = card.findChild(QToolButton, f"guidedSectionButton_{key}")
            assert button is not None and button.isChecked() is expanded
        assert len(card.findChildren(QWidget, "commandErrorPolicy")) == 1
        card.deleteLater()


def test_best_target_hides_settings_that_runtime_ignores() -> None:
    card, command, _editor = _card("click_best_target")

    def visible(name: str) -> bool:
        widget = card.findChild(QWidget, f"commandField_{name}")
        assert widget is not None
        row = widget.parentWidget()
        assert row is not None and row.objectName() == "guidedParameterRow"
        return (
            widget.property("presentationVisible") is True
            and row.property("presentationVisible") is True
        )

    assert visible("bonus_rule")
    assert visible("fallback_mode")
    assert visible("path_tolerance_px")
    assert visible("source_mode")
    assert visible("source_fallback")
    assert not visible("source_x")
    assert not visible("source_y")

    command.params["source_fallback"] = "fixed_position"
    card._rebuild_params()
    assert visible("source_x")
    assert visible("source_y")

    command.params["scoring_mode"] = "largest"
    card._rebuild_params()
    for name in (
        "bonus_rule",
        "fallback_mode",
        "path_tolerance_px",
        "source_mode",
        "source_fallback",
        "source_x",
        "source_y",
    ):
        assert not visible(name)

    command.params["scoring_mode"] = "nearest"
    command.params["source_mode"] = "fixed_position"
    card._rebuild_params()
    assert visible("source_mode")
    assert not visible("source_fallback")
    assert visible("source_x")
    assert visible("source_y")
    assert not visible("bonus_rule")
    assert not visible("fallback_mode")
    assert not visible("path_tolerance_px")
    card.deleteLater()


def test_visual_target_options_use_readable_labels_with_stable_values() -> None:
    command_types = load_command_types()
    best_definitions = {
        item["name"]: item
        for item in command_types["click_best_target"]["params"]
    }
    assert best_definitions["scoring_mode"]["option_labels"] == {
        "closest_to_bonus_path": "Bonus along path",
        "nearest": "Nearest to source",
        "largest": "Largest",
        "random": "Random",
    }
    assert best_definitions["fallback_mode"]["option_labels"]["none"] == (
        "Don't click"
    )
    assert best_definitions["source_mode"]["option_labels"] == {
        "last_clicked_target": "Last clicked target",
        "screen_center": "Screen center",
        "fixed_position": "Fixed position",
    }

    dynamic_definitions = {
        item["name"]: item
        for item in command_types["click_dynamic_target"]["params"]
    }
    assert dynamic_definitions["strategy"]["option_labels"] == {
        "latest": "Click where it is",
        "intercept": "Click when it enters an area",
        "predict": "Click ahead of it",
    }
    assert dynamic_definitions["prediction_fallback"]["option_labels"]["no_click"] == (
        "Do not click"
    )


def test_visual_target_diagnostics_document_generated_outputs() -> None:
    card, _command, _editor = _card("click_best_target")
    description = card.findChild(QLabel, "guidedSectionDescription_diagnostics")
    assert description is not None
    assert "_visual_*" in description.text()
    card.deleteLater()


def test_dynamic_target_reveals_only_strategy_specific_controls() -> None:
    card, command, _editor = _card("click_dynamic_target")

    def visible(name: str) -> bool:
        widget = card.findChild(QWidget, f"commandField_{name}")
        assert widget is not None
        row = widget.parentWidget()
        assert row is not None
        return (
            widget.property("presentationVisible") is True
            and row.property("presentationVisible") is True
        )

    assert visible("images")
    assert not visible("target_rule")
    assert not visible("trigger_start_x")
    assert not visible("lead_time_ms")

    command.params["strategy"] = "intercept"
    card._rebuild_params()
    assert visible("trigger_start_x")
    assert not visible("fixed_click_x")
    command.params["intercept_click"] = "fixed"
    card._rebuild_params()
    assert visible("fixed_click_x")

    command.params["strategy"] = "predict"
    card._rebuild_params()
    assert visible("lead_time_ms")
    assert not visible("trigger_start_x")

    command.params["target_source"] = "visual_rule"
    card._rebuild_params()
    assert not visible("images")
    assert visible("target_rule")
    card.deleteLater()


def test_dynamic_target_selectors_rebuild_conditional_fields() -> None:
    app = _app()
    card, _command, _editor = _card("click_dynamic_target")

    def visible(name: str) -> bool:
        widget = card.findChild(QWidget, f"commandField_{name}")
        assert widget is not None
        row = widget.parentWidget()
        assert row is not None
        return (
            widget.property("presentationVisible") is True
            and row.property("presentationVisible") is True
        )

    assert not visible("search_start_x")
    card._on_param_changed("search_region_mode", "fixed")
    app.processEvents()
    assert visible("search_coordinate_unit")
    assert visible("search_start_x")
    assert visible("search_start_y")
    assert visible("search_end_x")
    assert visible("search_end_y")

    assert visible("images")
    card._on_param_changed("target_source", "visual_rule")
    app.processEvents()
    assert not visible("images")
    assert not visible("threshold")
    assert visible("target_rule")

    card._on_param_changed("strategy", "intercept")
    app.processEvents()
    assert visible("trigger_start_x")
    card._on_param_changed("intercept_click", "fixed")
    app.processEvents()
    assert visible("fixed_click_x")
    card.deleteLater()


def test_image_command_family_reuses_exact_target_and_scope_sections() -> None:
    for command_id in (
        "click_image",
        "find_image",
        "if_image",
        "while",
        "win_click_image",
        "win_drag_image",
        "win_if_image",
        "win_while",
    ):
        sections = COMMAND_PRESENTATIONS[command_id].sections
        assert sections[0] is IMAGE_TARGET_SECTION
        assert next(section for section in sections if section.key == "scope") is (
            IMAGE_SCOPE_SECTION
        )


def test_find_image_documents_fixed_outputs_and_has_no_legacy_condition() -> None:
    command_types = load_command_types()
    parameter_names = {
        definition["name"] for definition in command_types["find_image"]["params"]
    }
    assert "condition" not in parameter_names

    card, _command, _editor = _card("find_image")
    result_button = card.findChild(QToolButton, "guidedSectionButton_result")
    result_description = card.findChild(
        QLabel, "guidedSectionDescription_result"
    )
    assert result_button is not None and result_button.isChecked()
    assert result_description is not None
    assert all(
        name in result_description.text()
        for name in ("_image_found", "_match_x", "_match_y", "_match_confidence")
    )
    card.deleteLater()


def test_read_screen_text_uses_approved_sections_and_defaults() -> None:
    card, _command, _editor = _card("read_screen_text")
    expected = {
        "capture_area": True,
        "result": True,
        "timing": False,
        "failure": False,
    }
    for key, expanded in expected.items():
        button = card.findChild(QToolButton, f"guidedSectionButton_{key}")
        assert button is not None and button.isChecked() is expanded
    assert len(card.findChildren(QWidget, "commandErrorPolicy")) == 1
    card.deleteLater()


def test_read_screen_text_capture_area_hides_irrelevant_mode_fields() -> None:
    card, command, _editor = _card("read_screen_text")

    def visible(name: str) -> bool:
        widget = card.findChild(QWidget, f"commandField_{name}")
        assert widget is not None
        row = widget.parentWidget()
        assert row is not None and row.objectName() == "guidedParameterRow"
        return (
            widget.property("presentationVisible") is True
            and row.property("presentationVisible") is True
        )

    assert visible("coordinate_unit")
    assert visible("start_x")
    assert visible("end_y")
    assert not visible("images")
    assert not visible("threshold")
    assert not visible("anchor")

    command.params["region_mode"] = "image_anchor"
    card._rebuild_params()
    assert visible("coordinate_unit")
    assert not visible("start_x")
    assert not visible("end_y")
    assert visible("images")
    assert visible("threshold")
    assert visible("color_match")
    assert not visible("color_tolerance")
    assert visible("anchor")
    assert visible("region_width")

    command.params["color_match"] = True
    card._rebuild_params()
    assert visible("color_tolerance")
    card.deleteLater()


def test_read_screen_text_result_fields_follow_selected_type() -> None:
    card, command, _editor = _card("read_screen_text")

    def visible(name: str) -> bool:
        widget = card.findChild(QWidget, f"commandField_{name}")
        assert widget is not None
        return widget.property("presentationVisible") is True

    assert visible("allow_empty")
    assert not visible("decimal_separator")

    command.params["result_type"] = "integer"
    card._rebuild_params()
    assert not visible("allow_empty")
    assert not visible("decimal_separator")
    parsing_label = next(
        item for item in card.findChildren(QLabel) if item.text() == "Parsing"
    )
    assert parsing_label.parentWidget().property("presentationVisible") is False

    command.params["result_type"] = "number"
    card._rebuild_params()
    assert not visible("allow_empty")
    assert visible("decimal_separator")

    command.params["result_type"] = "duration_ms"
    card._rebuild_params()
    assert not visible("decimal_separator")
    assert visible("duration_format")
    card.deleteLater()


def test_read_screen_text_options_use_readable_labels_with_stable_values() -> None:
    command_types = load_command_types()
    definitions = {
        item["name"]: item
        for item in command_types["read_screen_text"]["params"]
    }
    assert definitions["log_values"]["default"] is False
    assert definitions["log_values"]["label"] == "Include Values in Log"
    assert definitions["region_mode"]["option_labels"] == {
        "fixed": "Fixed area",
        "image_anchor": "Image-anchored area",
    }
    assert definitions["result_type"]["option_labels"] == {
        "text": "Text",
        "integer": "Whole number",
        "number": "Decimal number",
        "duration_ms": "Duration",
    }
    assert definitions["decimal_separator"]["option_labels"] == {
        "auto": "Auto-detect",
        "dot": "Dot (.)",
        "comma": "Comma (,)"
    }
    assert definitions["duration_format"]["option_labels"] == {
        "minutes_seconds": "Minutes : seconds",
        "hours_minutes": "Hours : minutes",
        "hours_minutes_seconds": "Hours : minutes : seconds",
    }
    assert definitions["recognition_mode"]["option_labels"] == {
        "text_block": "Text block",
        "single_value": "Single value",
    }
    assert definitions["recognition_mode"]["default"] == "text_block"
    assert definitions["value_match_mode"]["option_labels"] == {
        "exact": "Exact output",
        "first_match": "First matching value",
    }
    assert definitions["value_match_mode"]["default"] == "exact"
    assert definitions["preprocessing"]["option_labels"]["original"] == "Original"
    assert definitions["preprocessing"]["option_labels"]["bright_color_text"] == (
        "4× bright color text"
    )
    assert definitions["preprocessing"]["default"] == "original"


def test_image_condition_commands_use_readable_labels_with_stable_values() -> None:
    command_types = load_command_types()
    for command_id in ("if_image", "while", "win_if_image", "win_while"):
        condition = next(
            definition
            for definition in command_types[command_id]["params"]
            if definition["name"] == "condition"
        )
        assert condition["options"] == ["found", "not_found"]
        assert condition["option_labels"] == {
            "found": "Image is found",
            "not_found": "Image is not found",
        }


def test_image_family_uses_approved_section_defaults() -> None:
    expected_by_command = {
        "find_image": {
            "target": True,
            "result": True,
            "scope": False,
            "timing": False,
            "failure": False,
        },
        "if_image": {
            "target": True,
            "condition": True,
            "action": False,
            "scope": False,
            "timing": False,
            "failure": False,
        },
        "while": {
            "target": True,
            "condition": True,
            "scope": False,
            "timing": False,
            "failure": False,
        },
        "win_click_image": {
            "target": True,
            "action": True,
            "scope": False,
            "timing": False,
            "failure": False,
        },
        "win_drag_image": {
            "target": True,
            "action": True,
            "scope": False,
            "timing": False,
            "failure": False,
        },
        "win_if_image": {
            "target": True,
            "condition": True,
            "action": False,
            "scope": False,
            "timing": False,
            "failure": False,
        },
        "win_while": {
            "target": True,
            "condition": True,
            "action": False,
            "scope": False,
            "timing": False,
            "failure": False,
        },
    }
    for command_id, expected in expected_by_command.items():
        card, _command, _editor = _card(command_id)
        for key, expanded in expected.items():
            button = card.findChild(QToolButton, f"guidedSectionButton_{key}")
            assert button is not None
            assert button.isChecked() is expanded
        card.deleteLater()


def test_if_image_action_visibility_preserves_click_settings() -> None:
    card, command, _editor = _card("if_image")
    action = card.findChild(QWidget, "guidedSection_action")
    hold = card.findChild(QWidget, "commandField_hold_duration_ms")
    assert action is not None and action.property("presentationVisible") is True
    assert hold is not None and hold.property("presentationVisible") is False

    command.params["clickIfFound"] = True
    command.params["hold_duration_ms"] = 250
    command.params["xmargin"] = 12
    command.params["ymargin"] = -4
    card._rebuild_params()
    hold = card.findChild(QWidget, "commandField_hold_duration_ms")
    assert hold is not None and hold.property("presentationVisible") is True

    command.params["condition"] = "not_found"
    card._rebuild_params()
    action = card.findChild(QWidget, "guidedSection_action")
    assert action is not None and action.property("presentationVisible") is False
    assert command.params["clickIfFound"] is True
    assert command.params["hold_duration_ms"] == 250
    assert command.params["xmargin"] == 12
    assert command.params["ymargin"] == -4
    card.deleteLater()


def test_windows_conditional_action_visibility_matches_runtime_semantics() -> None:
    card, command, _editor = _card("win_if_image")
    command.params.update(
        {
            "click_if_found": True,
            "xmargin": 12,
            "ymargin": -4,
            "condition": "not_found",
        }
    )
    card._rebuild_params()
    action = card.findChild(QWidget, "guidedSection_action")
    assert action is not None and action.property("presentationVisible") is False
    assert command.params["click_if_found"] is True
    assert command.params["xmargin"] == 12
    assert command.params["ymargin"] == -4
    card.deleteLater()

    card, command, _editor = _card("win_while")
    command.params.update({"condition": "not_found", "click_if_found": True})
    card._rebuild_params()
    action = card.findChild(QWidget, "guidedSection_action")
    summary = card.findChild(QLabel, "guidedSectionSummary_action")
    assert action is not None and action.property("presentationVisible") is True
    assert summary is not None and summary.text() == "Click when image appears"
    card.deleteLater()


def test_windows_physical_timing_only_appears_for_physical_modes() -> None:
    for command_id in ("win_click_image", "win_if_image", "win_while"):
        card, command, _editor = _card(command_id)
        if command_id in {"win_if_image", "win_while"}:
            command.params["click_if_found"] = True
        command.params["mode"] = "message"
        card._rebuild_params()
        focus = card.findChild(QWidget, "commandField_focus_delay_ms")
        click_duration = card.findChild(
            QWidget, "commandField_click_duration_ms"
        )
        restore = card.findChild(QWidget, "commandField_restore_delay_ms")
        assert focus is not None and focus.property("presentationVisible") is False
        assert click_duration is not None
        assert click_duration.property("presentationVisible") is False
        assert restore is not None and restore.property("presentationVisible") is False

        command.params["mode"] = "input"
        card._rebuild_params()
        for field_name in (
            "focus_delay_ms",
            "click_duration_ms",
            "restore_delay_ms",
        ):
            field = card.findChild(QWidget, f"commandField_{field_name}")
            assert field is not None
            assert field.property("presentationVisible") is True
        card.deleteLater()

    card, command, _editor = _card("win_drag_image")
    command.params["mode"] = "message"
    card._rebuild_params()
    drag_duration = card.findChild(QWidget, "commandField_drag_duration_ms")
    drag_steps = card.findChild(QWidget, "commandField_drag_steps")
    focus = card.findChild(QWidget, "commandField_focus_delay_ms")
    assert drag_duration is not None
    assert drag_duration.property("presentationVisible") is True
    assert drag_steps is not None and drag_steps.property("presentationVisible") is True
    assert focus is not None and focus.property("presentationVisible") is False
    card.deleteLater()


def test_windows_timing_fields_use_compact_rows_that_fit_editor_width() -> None:
    for command_id in (
        "win_click_image",
        "win_drag_image",
        "win_if_image",
        "win_while",
    ):
        card, command, _editor = _card(command_id)
        command.params["mode"] = "input"
        if command_id in {"win_if_image", "win_while"}:
            command.params["click_if_found"] = True
        card._rebuild_params()
        for field_name in ("focus_delay_ms", "restore_delay_ms"):
            field = card.findChild(QWidget, f"commandField_{field_name}")
            assert field is not None and field.width() == 160
        if command_id != "win_drag_image":
            click_duration = card.findChild(
                QWidget, "commandField_click_duration_ms"
            )
            assert click_duration is not None and click_duration.width() == 160
        card.deleteLater()

    card, _command, _editor = _card("win_while")
    timeout = card.findChild(QWidget, "commandField_timeout")
    interval = card.findChild(QWidget, "commandField_interval_ms")
    pause = card.findChild(QWidget, "commandField_pause_ms")
    minimum = card.findChild(QWidget, "commandField_min_wait_time")
    assert timeout is not None and interval is not None
    assert pause is not None and minimum is not None
    assert timeout.parentWidget() is interval.parentWidget()
    assert pause.parentWidget() is minimum.parentWidget()
    assert timeout.parentWidget() is not pause.parentWidget()
    card.deleteLater()


def test_windows_image_options_use_approved_readable_labels() -> None:
    command_types = load_command_types()
    expected_modes = {
        "message": "Background message",
        "input": "Physical input",
        "cursor": "Cursor fallback",
    }
    for command_id in (
        "win_click_image",
        "win_drag_image",
        "win_if_image",
        "win_while",
    ):
        mode = next(
            definition
            for definition in command_types[command_id]["params"]
            if definition["name"] == "mode"
        )
        assert mode["option_labels"] == expected_modes


def test_guided_rows_hide_labels_when_every_field_is_irrelevant() -> None:
    card, command, _editor = _card("click_image")

    def row_for_label(text: str) -> QWidget:
        label = next(item for item in card.findChildren(QLabel) if item.text() == text)
        row = label.parentWidget()
        assert row is not None
        return row

    assert row_for_label("Fixed region start").property("presentationVisible") is False
    assert row_for_label("Fixed region end").property("presentationVisible") is False
    assert row_for_label("Anchor target").property("presentationVisible") is False

    command.params["search_region_mode"] = "fixed"
    card._rebuild_params()
    assert row_for_label("Fixed region start").property("presentationVisible") is True
    assert row_for_label("Fixed region end").property("presentationVisible") is True
    assert row_for_label("Anchor target").property("presentationVisible") is False
    card.deleteLater()

    card, _command, _editor = _card("if_image")
    click_details = next(
        item for item in card.findChildren(QLabel) if item.text() == "Click details"
    )
    click_details_row = click_details.parentWidget()
    assert click_details_row is not None
    assert click_details_row.property("presentationVisible") is False
    card.deleteLater()


def test_guided_family_has_no_visible_rows_without_visible_fields() -> None:
    states = (
        ("click_image", {}),
        ("click_image", {"search_region_mode": "fixed"}),
        ("click_image", {"search_region_mode": "image_anchor"}),
        (
            "click_image",
            {
                "search_region_mode": "image_anchor",
                "search_anchor_color_match": True,
            },
        ),
        ("find_image", {"not_found_behavior": "attention"}),
        ("if_image", {"clickIfFound": False}),
        ("if_image", {"clickIfFound": True}),
        ("if_image", {"condition": "not_found"}),
        ("if_image", {"condition_unmet_behavior": "attention"}),
        ("while", {"condition_unmet_behavior": "attention"}),
        ("win_click_image", {"mode": "message"}),
        ("win_click_image", {"mode": "input"}),
        ("win_drag_image", {"mode": "message"}),
        ("win_drag_image", {"mode": "input"}),
        ("win_if_image", {"click_if_found": False}),
        ("win_if_image", {"click_if_found": True, "mode": "message"}),
        ("win_if_image", {"click_if_found": True, "mode": "input"}),
        ("win_if_image", {"condition": "not_found"}),
        ("win_while", {"condition": "not_found", "click_if_found": True}),
        ("read_screen_text", {"region_mode": "fixed", "result_type": "text"}),
        (
            "read_screen_text",
            {"region_mode": "image_anchor", "color_match": False},
        ),
        (
            "read_screen_text",
            {
                "region_mode": "image_anchor",
                "color_match": True,
                "result_type": "number",
            },
        ),
    )
    for command_id, params in states:
        card, command, _editor = _card(command_id)
        command.params.update(params)
        card._rebuild_params()
        for row in card.findChildren(QWidget, "guidedParameterRow"):
            fields = [
                child
                for child in row.findChildren(QWidget)
                if child.objectName().startswith("commandField_")
            ]
            assert fields
            has_visible_field = any(
                field.property("presentationVisible") is True for field in fields
            )
            if not has_visible_field:
                assert row.property("presentationVisible") is False
                assert row.isHidden()
        card.deleteLater()


def test_embedded_error_policy_aligns_with_failure_parameter() -> None:
    app = _app()
    for command_id, behavior_name in (
        ("click_image", "not_found_behavior"),
        ("find_image", "not_found_behavior"),
        ("if_image", "condition_unmet_behavior"),
        ("while", "condition_unmet_behavior"),
        ("win_click_image", "not_found_behavior"),
        ("win_drag_image", "not_found_behavior"),
        ("win_if_image", "condition_unmet_behavior"),
        ("win_while", "condition_unmet_behavior"),
    ):
        card, _command, _editor = _card(command_id)
        card._toggle_collapsed()
        failure_button = card.findChild(
            QToolButton, "guidedSectionButton_failure"
        )
        assert failure_button is not None
        failure_button.setChecked(True)
        card.resize(900, 700)
        card.show()
        app.processEvents()

        behavior = card.findChild(QWidget, f"commandField_{behavior_name}")
        on_error_field = card.findChild(QWidget, "commandOnErrorField")
        assert behavior is not None and on_error_field is not None
        assert behavior.mapTo(card, QPoint(0, 0)).x() == on_error_field.mapTo(
            card, QPoint(0, 0)
        ).x()

        card.close()
        card.deleteLater()


def test_image_family_failure_options_use_command_specific_labels() -> None:
    command_types = load_command_types()
    expected = {
        "find_image": (
            "not_found_behavior",
            {
                "normal": 'Continue with "not found"',
                "fail": "Fail the flow",
                "attention": "Request attention",
            },
        ),
        "if_image": (
            "condition_unmet_behavior",
            {
                "normal": "Run Else branch",
                "fail": "Fail the flow",
                "attention": "Request attention",
            },
        ),
        "while": (
            "condition_unmet_behavior",
            {
                "normal": "End the loop",
                "fail": "Fail the flow",
                "attention": "Request attention",
            },
        ),
        "win_click_image": (
            "not_found_behavior",
            {
                "normal": "Use On Error setting",
                "fail": "Fail the flow",
                "attention": "Request attention",
            },
        ),
        "win_drag_image": (
            "not_found_behavior",
            {
                "normal": "Use On Error setting",
                "fail": "Fail the flow",
                "attention": "Request attention",
            },
        ),
        "win_if_image": (
            "condition_unmet_behavior",
            {
                "normal": "Run Else branch",
                "fail": "Fail the flow",
                "attention": "Request attention",
            },
        ),
        "win_while": (
            "condition_unmet_behavior",
            {
                "normal": "End the loop",
                "fail": "Fail the flow",
                "attention": "Request attention",
            },
        ),
    }
    for command_id, (parameter_name, labels) in expected.items():
        definition = next(
            item
            for item in command_types[command_id]["params"]
            if item["name"] == parameter_name
        )
        assert definition["option_labels"] == labels


def test_click_image_uses_guided_sections_with_approved_defaults() -> None:
    card, _command, _editor = _card("click_image")

    expected = {
        "target": True,
        "action": True,
        "scope": False,
        "timing": False,
        "failure": False,
    }
    for key, expanded in expected.items():
        assert card.findChild(QWidget, f"guidedSection_{key}") is not None
        button = card.findChild(QToolButton, f"guidedSectionButton_{key}")
        assert button is not None
        assert button.isChecked() is expanded

    assert card.findChild(QWidget, "commandErrorPolicy") is not None
    assert len(card.findChildren(QWidget, "commandErrorPolicy")) == 1
    card.deleteLater()


def test_click_image_construction_does_not_flash_parentless_windows() -> None:
    app = _app()
    recorder = _TopLevelShowRecorder()
    app.installEventFilter(recorder)
    try:
        card, _command, _editor = _card("click_image")
    finally:
        app.removeEventFilter(recorder)

    assert recorder.widgets == []
    card.deleteLater()


def test_color_match_boolean_rebuild_shows_tolerance_without_window_flash() -> None:
    app = _app()
    card, command, _editor = _card("click_image")
    color_field = card.findChild(QWidget, "commandField_color_match")
    assert color_field is not None
    source_picker = color_field.findChild(QComboBox, "valueReferencePicker")
    assert source_picker is not None
    boolean_literal_index = next(
        index
        for index in range(source_picker.count())
        if source_picker.itemData(index) == ("literal", "boolean")
    )

    recorder = _TopLevelShowRecorder()
    app.installEventFilter(recorder)
    try:
        source_picker.setCurrentIndex(boolean_literal_index)
        app.processEvents()
        color_field = card.findChild(QWidget, "commandField_color_match")
        assert color_field is not None
        boolean_editor = color_field.findChild(QComboBox, "literalBooleanEditor")
        assert boolean_editor is not None
        boolean_editor.setCurrentIndex(boolean_editor.findData(True))
        app.processEvents()
    finally:
        app.removeEventFilter(recorder)

    assert command.params["color_match"] == {
        "kind": "literal",
        "type": "boolean",
        "value": True,
    }
    tolerance = card.findChild(QWidget, "commandField_color_tolerance")
    assert tolerance is not None
    assert tolerance.property("presentationVisible") is True
    assert recorder.widgets == []
    card.deleteLater()


def test_click_image_hides_conditional_fields_without_dropping_values() -> None:
    card, command, _editor = _card("click_image")
    command.params["color_tolerance"] = {
        "kind": "literal",
        "type": "number",
        "value": 0.15,
    }
    command.params["search_start_x"] = {
        "kind": "literal",
        "type": "number",
        "value": 24,
    }

    color_tolerance = card.findChild(QWidget, "commandField_color_tolerance")
    search_start_x = card.findChild(QWidget, "commandField_search_start_x")
    assert color_tolerance is not None
    assert color_tolerance.property("presentationVisible") is False
    assert search_start_x is not None
    assert search_start_x.property("presentationVisible") is False

    command.params["color_match"] = {
        "kind": "literal",
        "type": "boolean",
        "value": True,
    }
    command.params["search_region_mode"] = "fixed"
    card._rebuild_params()

    color_tolerance = card.findChild(QWidget, "commandField_color_tolerance")
    search_start_x = card.findChild(QWidget, "commandField_search_start_x")
    assert color_tolerance is not None
    assert color_tolerance.property("presentationVisible") is True
    assert search_start_x is not None
    assert search_start_x.property("presentationVisible") is True

    command.params["color_match"] = {
        "kind": "literal",
        "type": "boolean",
        "value": False,
    }
    command.params["search_region_mode"] = "full"
    card._rebuild_params()

    color_tolerance = card.findChild(QWidget, "commandField_color_tolerance")
    search_start_x = card.findChild(QWidget, "commandField_search_start_x")
    assert color_tolerance is not None
    assert color_tolerance.property("presentationVisible") is False
    assert search_start_x is not None
    assert search_start_x.property("presentationVisible") is False
    assert command.params["color_tolerance"]["value"] == 0.15
    assert command.params["search_start_x"]["value"] == 24
    card.deleteLater()


def test_guided_rows_top_align_fields_and_normalize_click_offset_widths() -> None:
    app = _app()
    card, _command, _editor = _card("click_image")
    card._toggle_collapsed()
    card.resize(1000, 800)
    card.show()
    app.processEvents()

    confidence = card.findChild(QWidget, "commandField_threshold")
    color_match = card.findChild(QWidget, "commandField_color_match")
    click_type = card.findChild(QWidget, "commandField_click")
    hold_duration = card.findChild(QWidget, "commandField_hold_duration_ms")
    offset_unit = card.findChild(QWidget, "commandField_offset_unit")
    offset_x = card.findChild(QWidget, "commandField_xmargin")
    offset_y = card.findChild(QWidget, "commandField_ymargin")

    assert confidence is not None and color_match is not None
    assert confidence.parentWidget() is color_match.parentWidget()
    assert confidence.geometry().top() == color_match.geometry().top()
    assert click_type is not None and hold_duration is not None
    assert click_type.parentWidget() is hold_duration.parentWidget()
    assert click_type.geometry().top() == hold_duration.geometry().top()
    assert offset_unit is not None and offset_x is not None and offset_y is not None
    assert {offset_unit.width(), offset_x.width(), offset_y.width()} == {160}

    card.close()
    card.deleteLater()


def test_guided_section_state_survives_rebuild_but_is_not_persisted() -> None:
    card, command, editor = _card("click_image")
    scope_button = card.findChild(QToolButton, "guidedSectionButton_scope")
    assert scope_button is not None

    scope_button.setChecked(True)
    assert editor.section_states[(command.step_id, "scope")] is True

    card._rebuild_params()
    rebuilt_scope = card.findChild(QToolButton, "guidedSectionButton_scope")
    assert rebuilt_scope is not None
    assert rebuilt_scope.isChecked()
    assert "section" not in str(command.to_dict()).lower()
    card.deleteLater()


def test_wait_uses_one_compact_row_without_guided_section_chrome() -> None:
    card, _command, _editor = _card("wait")
    duration = card.findChild(QWidget, "commandField_ms")
    pause = card.findChild(QWidget, "commandField_pause_ms")

    assert duration is not None
    assert pause is not None
    assert duration.maximumWidth() == 250
    assert pause.maximumWidth() == 250
    assert card.findChild(QWidget, "guidedSection_target") is None
    assert card.findChild(QWidget, "commandErrorPolicy") is not None
    card.deleteLater()
