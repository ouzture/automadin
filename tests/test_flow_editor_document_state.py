from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QLineEdit, QPushButton

from automadin.engine.session import DeviceSessionService
from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.models.flow_value import FlowInputDefinition
from automadin.models.typed_value import ValueType
from automadin.services.database import DatabaseService
from automadin.ui.flow_editor.editor_view import FlowEditorView
from automadin.ui.flow_editor.palette import CommandPaletteDialog


class FlowEditorDocumentStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db = DatabaseService(str(Path(self._temp_dir.name) / "editor.db"))
        self.editor = FlowEditorView(self.db, DeviceSessionService())

    def tearDown(self) -> None:
        self.editor.deleteLater()
        self.db.close()
        self._temp_dir.cleanup()

    def test_blank_document_starts_clean(self) -> None:
        self.assertFalse(self.editor.has_unsaved_changes)

    def test_command_mutation_marks_document_dirty(self) -> None:
        self.editor._flow.commands.append(FlowCommand("wait"))
        self.assertTrue(self.editor.has_unsaved_changes)

    def test_make_command_uses_shared_metadata_defaults(self) -> None:
        self.editor._command_types["example"] = {
            "id": "example",
            "params": [
                {"name": "label", "type": "string"},
                {"name": "pause_ms", "type": "int", "optional": True},
                {
                    "name": "mode",
                    "type": "enum",
                    "options": ["first", "second"],
                    "default": "second",
                },
            ],
        }

        command = self.editor._make_command("example")

        self.assertEqual(command.params, {"label": "", "mode": "second"})

    def test_web_inspector_adds_and_focuses_selected_locator_step(self) -> None:
        locator = {
            "platform": "browser",
            "strategy": "css",
            "value": "#results",
        }

        self.editor._add_inspected_locator_step("wait_for_selector", locator)

        self.assertEqual(len(self.editor._flow.commands), 1)
        command = self.editor._flow.commands[0]
        self.assertEqual(command.id, "wait_for_selector")
        self.assertEqual(command.params["locator"], locator)
        self.assertEqual(self.editor._selected_step_id, command.step_id)
        self.assertTrue(self.editor.has_unsaved_changes)

    def test_make_switch_value_creates_one_case_and_permanent_default(self) -> None:
        command = self.editor._make_command("switch_value")

        self.assertEqual(
            [branch.id for branch in command.children],
            ["switch_case", "switch_default"],
        )
        self.assertEqual(command.children[0].params["operator"], "equals")
        self.assertFalse(command.children[0].params["ignore_case"])
        self.assertFalse(command.children[0].collapsed)

    def test_switch_case_operations_preserve_default_last_and_one_case(self) -> None:
        command = self.editor._make_command("switch_value")
        self.editor._flow.commands = [command]
        first = command.children[0]

        second = self.editor._add_switch_case(command)
        self.assertEqual(
            [branch.id for branch in command.children],
            ["switch_case", "switch_case", "switch_default"],
        )
        self.editor._move_switch_case(second, -1)
        self.assertIs(command.children[0], second)
        self.editor._remove_switch_case(second)
        self.assertEqual(
            [branch.id for branch in command.children],
            ["switch_case", "switch_default"],
        )

        self.editor._remove_switch_case(first)
        self.assertEqual(
            [branch.id for branch in command.children],
            ["switch_case", "switch_default"],
        )

    def test_duplicate_switch_case_inserts_deep_copy_immediately_below(self) -> None:
        command = self.editor._make_command("switch_value")
        self.editor._flow.commands = [command]
        original = command.children[0]
        original.params["right"] = {
            "kind": "literal",
            "type": "number",
            "value": 3,
        }
        original.comment = "Premium customer"
        original.collapsed = True
        original.children = [FlowCommand("wait", params={"ms": 250})]

        duplicate = self.editor._duplicate_switch_case(original)

        self.assertIsNotNone(duplicate)
        self.assertIs(command.children[1], duplicate)
        self.assertEqual(
            [branch.id for branch in command.children],
            ["switch_case", "switch_case", "switch_default"],
        )
        self.assertEqual(duplicate.params, original.params)
        self.assertEqual(duplicate.comment, original.comment)
        self.assertFalse(duplicate.collapsed)
        self.assertNotEqual(duplicate.step_id, original.step_id)
        self.assertNotEqual(
            duplicate.children[0].step_id,
            original.children[0].step_id,
        )

    def test_structural_switch_branches_are_hidden_from_global_palette(self) -> None:
        dialog = CommandPaletteDialog(self.editor._command_types)
        try:
            visible_ids = {definition["id"] for _tile, definition in dialog._tiles}
            self.assertIn("switch_value", visible_ids)
            self.assertNotIn("switch_case", visible_ids)
            self.assertNotIn("switch_default", visible_ids)
        finally:
            dialog.deleteLater()

    def test_switch_card_uses_contextual_case_and_default_controls(self) -> None:
        command = self.editor._make_command("switch_value")
        command.collapsed = False
        for branch in command.children:
            branch.collapsed = False
        self.editor._flow.commands = [command]
        self.editor._rebuild_cards()
        self.app.processEvents()

        card = self.editor._card_by_step_id[command.step_id]
        add_case = card.findChild(QPushButton, "addSwitchCaseButton")
        labels = {
            label.text()
            for label in card.findChildren(QLabel, "switchBranchLabel")
        }

        self.assertIsNotNone(add_case)
        self.assertEqual(labels, {"Case 1", "Default"})
        outline_switch = self.editor._outline_tree.topLevelItem(0)
        self.assertEqual(
            [outline_switch.child(index).text(1) for index in range(outline_switch.childCount())],
            ["Case 1", "Default"],
        )
        self.assertFalse(
            any(button.text() == "Remove" and button.isEnabled() for button in card.findChildren(QPushButton))
        )

        add_case.click()
        self.app.processEvents()
        self.assertEqual(
            [branch.id for branch in command.children],
            ["switch_case", "switch_case", "switch_default"],
        )

    def test_switch_branch_controls_are_explicit_and_cases_open_for_editing(self) -> None:
        command = self.editor._make_command("switch_value")
        command.collapsed = False
        self.editor._flow.commands = [command]
        self.editor._rebuild_cards()
        self.app.processEvents()

        case = command.children[0]
        case_card = self.editor._card_by_step_id[case.step_id]
        actions_button = case_card.findChild(QPushButton, "switchCaseActionsButton")
        action_texts = {action.text() for action in actions_button.menu().actions()}

        self.assertFalse(case.collapsed)
        self.assertFalse(case_card._params_container.isHidden())
        self.assertEqual(case_card._drag_handle.text(), "\u2630")
        self.assertEqual(case_card._drag_handle.toolTip(), "Drag to reorder Case")
        self.assertEqual(
            action_texts,
            {"Duplicate", "Move Up", "Move Down", "", "Remove"},
        )
        self.assertEqual(case_card._expand_btn.text(), "\u25b4")

    def test_switch_case_header_summarizes_condition_and_explanation(self) -> None:
        command = self.editor._make_command("switch_value")
        command.collapsed = False
        case = command.children[0]
        case.params.update(
            {
                "operator": "greater_than",
                "right": {"kind": "literal", "type": "number", "value": 42},
            }
        )
        case.comment = "High-value order"
        case.collapsed = True
        self.editor._flow.commands = [command]
        self.editor._rebuild_cards()
        self.app.processEvents()

        case_card = self.editor._card_by_step_id[case.step_id]
        condition = case_card.findChild(QLabel, "switchCaseConditionLabel")
        explanation = case_card.findChild(QLabel, "switchCaseExplanationLabel")
        explanation_edit = case_card.findChild(QLineEdit, "switchCaseExplanationEdit")

        self.assertEqual(condition.text(), "Is greater than 42")
        self.assertEqual(explanation.text(), "— High-value order")
        self.assertFalse(explanation.isHidden())
        self.assertTrue(explanation_edit.isHidden())

        case_card._toggle_collapsed()
        self.assertFalse(explanation_edit.isHidden())
        explanation_edit.setText("Routes priority orders")
        self.app.processEvents()
        self.assertEqual(case.comment, "Routes priority orders")
        self.assertEqual(explanation.text(), "— Routes priority orders")

        case_card._expand_btn.click()
        self.assertTrue(case.collapsed)
        self.assertTrue(explanation_edit.isHidden())

    def test_switch_and_branch_collapse_state_survives_serialization(self) -> None:
        switch = self.editor._make_command("switch_value")
        switch.collapsed = True
        switch.children[0].collapsed = False
        switch.children[-1].collapsed = True
        flow = AutomationFlow("Switch state", [switch])

        restored = AutomationFlow.from_dict(flow.to_dict()).commands[0]

        self.assertTrue(restored.collapsed)
        self.assertFalse(restored.children[0].collapsed)
        self.assertTrue(restored.children[-1].collapsed)

    def test_switch_collapse_hides_all_branches_independently(self) -> None:
        switch = self.editor._make_command("switch_value")
        switch.collapsed = False
        self.editor._flow.commands = [switch]
        self.editor._rebuild_cards()
        self.app.processEvents()

        switch_card = self.editor._card_by_step_id[switch.step_id]
        case = switch.children[0]
        self.assertFalse(case.collapsed)
        self.assertFalse(switch_card._children_container.isHidden())

        switch_card._expand_btn.click()

        self.assertTrue(switch.collapsed)
        self.assertTrue(switch_card._children_container.isHidden())
        self.assertFalse(case.collapsed)

    def test_switch_case_drop_targets_are_limited_to_the_same_switch(self) -> None:
        first_switch = self.editor._make_command("switch_value")
        second_switch = self.editor._make_command("switch_value")
        second_case = self.editor._add_switch_case(second_switch)
        self.editor._flow.commands = [first_switch, second_switch]
        self.editor._rebuild_cards()
        self.app.processEvents()

        first_case = first_switch.children[0]
        first_card = self.editor._card_by_step_id[first_case.step_id]
        second_card = self.editor._card_by_step_id[second_switch.children[0].step_id]
        sibling_card = self.editor._card_by_step_id[second_case.step_id]
        default_card = self.editor._card_by_step_id[second_switch.children[-1].step_id]

        self.assertFalse(first_card._can_accept_drop(first_case))
        self.assertFalse(second_card._can_accept_drop(first_case))
        self.assertTrue(sibling_card._can_accept_drop(second_switch.children[0]))
        self.assertTrue(default_card._can_accept_drop(second_switch.children[0]))
        self.assertNotEqual(sibling_card.styleSheet(), default_card.styleSheet())

    def test_switch_case_picker_filters_for_compatible_source_types(self) -> None:
        source_id = "10000000-0000-4000-8000-000000000001"
        self.editor._flow.inputs = [
            FlowInputDefinition("amount", ValueType.NUMBER, 1.5, id=source_id)
        ]
        command = self.editor._make_command("switch_value")
        command.params["source"] = {"kind": "input", "id": source_id}
        command.collapsed = False
        case = command.children[0]
        case.collapsed = False
        self.editor._flow.commands = [command]
        self.editor._rebuild_cards()
        self.app.processEvents()

        case_card = self.editor._card_by_step_id[case.step_id]
        picker = case_card.findChild(QComboBox, "valueReferencePicker")
        self.assertIsNotNone(picker)
        options = {picker.itemData(index) for index in range(picker.count())}

        self.assertIn(("literal", "integer"), options)
        self.assertIn(("literal", "number"), options)
        self.assertNotIn(("literal", "text"), options)

    def test_switch_movement_guards_keep_structural_branches_valid(self) -> None:
        switch = self.editor._make_command("switch_value")
        ordinary = FlowCommand("wait", params={"ms": 1})
        self.editor._flow.commands = [switch, ordinary]

        self.editor._move_command_to_parent(ordinary.step_id, switch)
        self.assertEqual(self.editor._flow.commands, [switch, ordinary])
        self.assertEqual(
            [branch.id for branch in switch.children],
            ["switch_case", "switch_default"],
        )

        case_child = FlowCommand("wait", params={"ms": 2})
        switch.children[0].children.append(case_child)
        self.editor._move_out_of_parent(case_child)

        self.assertEqual(switch.children[0].children, [])
        self.assertEqual(self.editor._flow.commands, [switch, case_child, ordinary])

        case = switch.children[0]
        self.editor._move_command_to_root_end(case.step_id)
        self.assertIn(case, switch.children)
        self.assertNotIn(case, self.editor._flow.commands)

    def test_mark_clean_resets_snapshot_and_dirty_marker(self) -> None:
        self.editor._flow.commands.append(FlowCommand("wait"))
        self.editor._mark_clean()
        self.assertFalse(self.editor.has_unsaved_changes)
        self.assertFalse(self.editor._flow_name_label.text().endswith("*"))

    def test_rename_updates_identity_without_hiding_unsaved_commands(self) -> None:
        self.editor._flow.name = "Folder/Old"
        self.editor._mark_clean()
        self.editor._flow.commands.append(FlowCommand("wait"))

        self.editor._apply_renamed_flow_name("Folder/New")

        self.assertEqual(self.editor._flow.name, "Folder/New")
        self.assertTrue(self.editor.has_unsaved_changes)
        self.editor._flow.commands.clear()
        self.assertFalse(self.editor.has_unsaved_changes)

    def test_external_resource_move_updates_open_dirty_flow_and_baseline(self) -> None:
        self.editor._flow.name = "Caller"
        self.editor._flow.commands = [
            FlowCommand("run_flow", params={"flow_name": "Folder/Old"})
        ]
        self.editor._mark_clean()
        self.editor._flow.commands.append(FlowCommand("wait"))

        self.editor.on_resource_renamed("flow", "Folder/Old", "Folder/New")

        self.assertEqual(
            self.editor._flow.commands[0].params["flow_name"], "Folder/New"
        )
        self.assertTrue(self.editor.has_unsaved_changes)
        self.editor._flow.commands.pop()
        self.assertFalse(self.editor.has_unsaved_changes)

    def test_busy_command_summary_is_short_and_reserves_branch_count(self) -> None:
        command = FlowCommand(
            "if_image",
            params={
                "images": [
                    "iom_phone/shop_menu_needs_swipe_down",
                    "iom_phone/shop_menu_needs_swipe_down2",
                    "iom_phone/shop_menu_needs_swipe_down3",
                ],
                "threshold": 0.9,
                "condition": "found",
                "timeout": 5000,
            },
            children=[FlowCommand("wait")],
            else_children=[FlowCommand("wait")],
        )
        self.editor._flow.commands.append(command)
        self.editor._rebuild_cards()

        parts = self.editor._card_by_step_id[command.step_id]._summary_parts()

        self.assertLessEqual(len(parts), 3)
        self.assertIn("1 then step", parts)
        self.assertIn("1 else step", parts)

    def test_long_image_paths_are_reduced_to_the_filename(self) -> None:
        from automadin.ui.flow_editor.command_card import CommandCard

        value = CommandCard._short_summary_text(
            "iom_phone/a_very_long_folder/shop_menu_needs_swipe_down", 32
        )
        self.assertEqual(value, "…/shop_menu_needs_swipe_down")


if __name__ == "__main__":
    unittest.main()
