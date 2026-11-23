"""Focused editors for flow-owned typed conditional hooks."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from automadin.execution.handlers.registry import get_default_params
from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.models.builtin_scope import (
    IMAGE_MATCH_BUILTIN_TYPES,
    builtin_types_for_step,
    extend_builtin_types,
)
from automadin.models.hook import (
    AutomationHook,
    BrowserElementHookCondition,
    HookAutomaticScope,
    HookMatch,
    HookPhase,
    HookRetriggerPolicy,
    HookType,
    VisualImageHookCondition,
)
from automadin.models.value_reference import BuiltinValueReference
from automadin.ui.flow_editor.palette import CommandPaletteDialog
from automadin.ui.flow_editor.command_card import CommandCard
from automadin.ui.flow_editor.command_presentation import (
    CommandPresentation,
    IMAGE_SCOPE_SECTION,
    IMAGE_TARGET_SECTION,
    presentation_parameter_names,
)
from automadin.ui.flow_editor.param_editor import ParamEditorWidget


class _CommandEditorContext:
    """Small adapter that lets dialogs use the normal command renderer."""

    def __init__(
        self,
        flow: AutomationFlow,
        commands: list[FlowCommand],
        *,
        base_builtin_types: dict[str, Any] | None = None,
        feature_host: QWidget | None = None,
    ) -> None:
        self._flow = copy.copy(flow)
        self._flow.commands = commands
        self._flow.hooks = []
        self._base_builtin_types = dict(base_builtin_types or {})
        self._hooks_enabled = False
        self._feature_host = feature_host
        self._command_section_states: dict[tuple[str, str], bool] = {}

    def __getattr__(self, name: str) -> Any:
        if name not in {
            "_create_flow_variable_for_types",
            "_launch_capture_target",
            "_launch_dynamic_target_recorder",
            "_launch_visual_rule_editor",
            "_launch_android_package_picker",
        }:
            raise AttributeError(name)
        current = self._feature_host
        while current is not None:
            callback = getattr(current, name, None)
            if callable(callback):
                return callback
            current = current.parentWidget()
        raise AttributeError(name)

    def _register_command_card(self, _step_id: str, _card: CommandCard) -> None:
        return

    def _command_section_expanded(
        self,
        step_id: str,
        section_key: str,
        default: bool,
    ) -> bool:
        return self._command_section_states.get((step_id, section_key), default)

    def _set_command_section_expanded(
        self,
        step_id: str,
        section_key: str,
        expanded: bool,
    ) -> None:
        self._command_section_states[(step_id, section_key)] = expanded

    def _on_command_metadata_changed(self, _command: FlowCommand) -> None:
        return

    def _rebuild_cards(self) -> None:
        return

    def _find_parent_command(
        self,
        commands: list[FlowCommand],
        target: FlowCommand,
    ) -> FlowCommand | None:
        for command in commands:
            if any(child.step_id == target.step_id for child in command.children):
                return command
            if any(child.step_id == target.step_id for child in command.else_children):
                return command
            found = self._find_parent_command(command.children, target)
            if found is not None:
                return found
            found = self._find_parent_command(command.else_children, target)
            if found is not None:
                return found
        return None


class _CommandEditDialog(QDialog):
    def __init__(
        self,
        command: FlowCommand,
        command_types: dict[str, dict[str, Any]],
        flow: AutomationFlow,
        db: Any,
        parent: QWidget,
        *,
        action_roots: list[FlowCommand],
        base_builtin_types: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Configure {command_types[command.id]['label']}")
        self.resize(760, 720)
        self.setMinimumSize(560, 460)
        self._command = command
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setObjectName("hookCommandScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        context = _CommandEditorContext(
            flow,
            action_roots,
            base_builtin_types=base_builtin_types,
            feature_host=parent,
        )
        card = CommandCard(
            command,
            command_types,
            context,
            db=db,
            parameter_only=True,
        )
        scroll.setWidget(card)
        self.command_card = card
        self.command_scroll = scroll
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class HookEditDialog(QDialog):
    """Guided editor for one hook and its ordered action tree."""

    def __init__(
        self,
        flow: AutomationFlow,
        command_types: dict[str, dict[str, Any]],
        db: Any,
        *,
        hook: AutomationHook | None = None,
        hook_type: HookType = HookType.VISUAL_IMAGE,
        test_condition: Callable[[AutomationHook], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Hook" if hook else "New Hook")
        self.resize(720, 760)
        self._flow = flow
        self._command_types = command_types
        self._db = db
        self._original = hook
        self._test_condition_callback = test_condition
        self._type = hook.type if hook else hook_type
        self._actions = (
            copy.deepcopy(hook.actions) if hook else []
        )
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(hook.name if hook else "")
        self.name_edit.setPlaceholderText(
            "Captcha detected" if self._type is HookType.VISUAL_IMAGE else "Session expired"
        )
        form.addRow("Name", self.name_edit)
        self.description_edit = QLineEdit(hook.description if hook else "")
        form.addRow("Description", self.description_edit)
        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(hook.enabled if hook else True)
        form.addRow("", self.enabled_check)
        root.addLayout(form)

        tabs = QTabWidget()
        condition_page = QWidget()
        condition_page_layout = QVBoxLayout(condition_page)
        condition_page_layout.setContentsMargins(0, 0, 0, 0)
        condition_scroll = QScrollArea()
        condition_scroll.setObjectName("hookConditionScrollArea")
        condition_scroll.setWidgetResizable(True)
        condition_host = QWidget()
        condition_layout = QVBoxLayout(condition_host)
        condition_scroll.setWidget(condition_host)
        condition_page_layout.addWidget(condition_scroll)
        condition = hook.condition if hook else None
        self._condition_values: dict[str, Any] = {}
        if self._type is HookType.VISUAL_IMAGE:
            visual = condition if isinstance(condition, VisualImageHookCondition) else None
            condition_presentation = CommandPresentation(
                layout="guided",
                sections=(IMAGE_TARGET_SECTION, IMAGE_SCOPE_SECTION),
            )
            field_names = set(
                presentation_parameter_names(condition_presentation)
            )
            condition_definition = copy.deepcopy(command_types["find_image"])
            condition_definition["id"] = "visual_hook_condition"
            condition_definition["label"] = "Visual Image Condition"
            condition_definition["params"] = [
                definition
                for definition in condition_definition.get("params", [])
                if definition.get("name") in field_names
            ]
            self._condition_values = get_default_params(condition_definition)
            if visual is not None:
                self._condition_values.update(visual.match_params())
            else:
                self._condition_values["images"] = []
            self._condition_command = FlowCommand(
                "visual_hook_condition",
                params=self._condition_values,
                collapsed=False,
            )
            condition_types = dict(command_types)
            condition_types["visual_hook_condition"] = condition_definition
            condition_context = _CommandEditorContext(
                flow,
                [self._condition_command],
                feature_host=self,
            )
            condition_layout.addWidget(
                CommandCard(
                    self._condition_command,
                    condition_types,
                    condition_context,
                    db=db,
                    parameter_only=True,
                    presentation_override=condition_presentation,
                )
            )
        else:
            browser = condition if isinstance(condition, BrowserElementHookCondition) else None
            self._condition_values = {"locator": browser.locator if browser else ""}
            definitions = (
                {"name": "locator", "type": "locator", "label": "Element locator"},
            )
            for definition in definitions:
                condition_layout.addWidget(
                    ParamEditorWidget(
                        definition,
                        self._condition_values.get(definition["name"]),
                        self._set_condition,
                        db=db,
                        value_catalog=flow.value_catalog,
                    )
                )
        self.match_combo = QComboBox()
        self.match_combo.addItem("Condition is found / visible", HookMatch.FOUND.value)
        self.match_combo.addItem("Condition is not found / not visible", HookMatch.NOT_FOUND.value)
        current_match = condition.match if condition else HookMatch.FOUND
        self.match_combo.setCurrentIndex(0 if current_match is HookMatch.FOUND else 1)
        condition_layout.addWidget(QLabel("Match when"))
        condition_layout.addWidget(self.match_combo)
        if test_condition is not None:
            test_button = QPushButton("Test condition now…")
            test_button.clicked.connect(self._test_condition_now)
            condition_layout.addWidget(test_button)
        condition_layout.addStretch()
        tabs.addTab(condition_page, "Condition")

        action_page = QWidget()
        action_layout = QVBoxLayout(action_page)
        if self._type is HookType.VISUAL_IMAGE:
            match_help = QLabel(
                "For a Found condition, action fields can use Built-in values "
                "_match_x, _match_y, _match_confidence, and _matched_image. "
                "Use Click Position with _match_x and _match_y to click the "
                "observation without searching again."
            )
            match_help.setObjectName("hookMatchBuiltinsHelp")
            match_help.setWordWrap(True)
            action_layout.addWidget(match_help)
        self.actions_tree = QTreeWidget()
        self.actions_tree.setHeaderLabels(["Command", "Branch"])
        self.actions_tree.setColumnWidth(0, 420)
        self.actions_tree.itemDoubleClicked.connect(
            lambda _item, _column: self._edit_action()
        )
        action_layout.addWidget(self.actions_tree, 1)
        action_buttons = QHBoxLayout()
        if (
            self._type is HookType.VISUAL_IMAGE
            and flow.value_model_version == 1
        ):
            click_match = QPushButton("Add click match…")
            click_match.setToolTip(
                "Add a normal Click Position action using _match_x and _match_y."
            )
            click_match.clicked.connect(self._add_click_match_action)
            click_match.setEnabled(
                HookMatch(self.match_combo.currentData()) is HookMatch.FOUND
            )
            self.match_combo.currentIndexChanged.connect(
                lambda _index, button=click_match: button.setEnabled(
                    HookMatch(self.match_combo.currentData()) is HookMatch.FOUND
                )
            )
            action_buttons.addWidget(click_match)
        for label, callback in (
            ("Add root…", self._add_action),
            ("Add child…", self._add_child_action),
            ("Add else…", self._add_else_action),
            ("Edit", self._edit_action),
            ("Remove", self._remove_action),
            ("Up", lambda: self._move_action(-1)),
            ("Down", lambda: self._move_action(1)),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            action_buttons.addWidget(button)
        action_buttons.addStretch()
        action_layout.addLayout(action_buttons)
        tabs.addTab(action_page, "Actions")
        root.addWidget(tabs, 1)

        options = QFormLayout()
        self.scope_combo = QComboBox()
        for label, value in (
            ("Only when explicitly checked", HookAutomaticScope.NONE),
            ("Selected commands", HookAutomaticScope.SELECTED_COMMANDS),
            ("All compatible commands", HookAutomaticScope.ALL_COMPATIBLE_COMMANDS),
        ):
            self.scope_combo.addItem(label, value.value)
        scope = hook.automatic_scope if hook else HookAutomaticScope.NONE
        self.scope_combo.setCurrentIndex(list(HookAutomaticScope).index(scope))
        options.addRow("Automatic scope", self.scope_combo)
        phases = hook.automatic_phases if hook else (HookPhase.BEFORE,)
        phase_row = QWidget()
        phase_layout = QHBoxLayout(phase_row)
        phase_layout.setContentsMargins(0, 0, 0, 0)
        self.before_check = QCheckBox("Before action")
        self.before_check.setChecked(HookPhase.BEFORE in phases)
        self.after_check = QCheckBox("After action")
        self.after_check.setChecked(HookPhase.AFTER in phases)
        phase_layout.addWidget(self.before_check)
        phase_layout.addWidget(self.after_check)
        options.addRow("Global phases", phase_row)
        self.retrigger_combo = QComboBox()
        self.retrigger_combo.addItem("Re-arm after absent", HookRetriggerPolicy.REARM_AFTER_ABSENT.value)
        self.retrigger_combo.addItem("Once per run", HookRetriggerPolicy.ONCE_PER_RUN.value)
        retrigger = hook.retrigger if hook else HookRetriggerPolicy.REARM_AFTER_ABSENT
        self.retrigger_combo.setCurrentIndex(list(HookRetriggerPolicy).index(retrigger))
        options.addRow("Re-trigger", self.retrigger_combo)
        root.addLayout(options)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_valid)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._refresh_actions()

    def _set_condition(self, name: str, value: Any) -> None:
        self._condition_values[name] = value

    def _condition(self) -> VisualImageHookCondition | BrowserElementHookCondition:
        if self._type is HookType.VISUAL_IMAGE:
            primary_names = {
                "images",
                "threshold",
                "color_match",
                "color_tolerance",
            }
            search_settings = (
                copy.deepcopy(dict(self._original.condition.search_settings))
                if self._original
                and isinstance(self._original.condition, VisualImageHookCondition)
                else {}
            )
            search_settings.update(
                {
                    name: copy.deepcopy(value)
                    for name, value in self._condition_values.items()
                    if name not in primary_names
                }
            )
            return VisualImageHookCondition(
                tuple(self._condition_values.get("images", [])),
                HookMatch(self.match_combo.currentData()),
                self._condition_values.get("threshold", 0.9),
                self._condition_values.get("color_match", False),
                self._condition_values.get("color_tolerance", 0.3),
                search_settings,
            )
        return BrowserElementHookCondition(
            self._condition_values.get("locator", ""),
            HookMatch(self.match_combo.currentData()),
        )

    def _test_condition_now(self) -> None:
        callback = self._test_condition_callback
        if callback is None:
            return
        try:
            condition = self._condition()
            hook = AutomationHook(
                name=self.name_edit.text().strip() or "Unsaved hook",
                condition=condition,
                actions=[],
                automatic_scope=HookAutomaticScope.NONE,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Hook Condition", str(exc))
            return
        callback(hook)

    def _action_candidates(self) -> dict[str, dict[str, Any]]:
        return {
            command_id: definition
            for command_id, definition in self._command_types.items()
            if command_id not in {"check_hooks", "switch_case", "switch_default"}
        }

    def _add_action(self) -> None:
        self._add_to(
            self._actions,
            base_builtin_types=self._hook_action_builtin_types(),
        )

    def _add_click_match_action(self) -> None:
        command = self._make_command("click_position")
        command.params["coordinate_unit"] = "pixels"
        command.params["x"] = BuiltinValueReference("_match_x").to_dict()
        command.params["y"] = BuiltinValueReference("_match_y").to_dict()
        edit = _CommandEditDialog(
            command,
            self._command_types,
            self._flow,
            self._db,
            self,
            action_roots=[command],
            base_builtin_types=self._hook_action_builtin_types(),
        )
        if edit.exec() == QDialog.DialogCode.Accepted:
            self._actions.append(command)
            self._refresh_actions(command.step_id)

    def _add_child_action(self) -> None:
        command = self._selected_action()
        if command is None:
            QMessageBox.information(
                self,
                "Select a Parent",
                "Select a container command before adding a child action.",
            )
            return
        definition = self._command_types.get(command.id, {})
        if not definition.get("canHaveChildren", False):
            QMessageBox.information(
                self,
                "Children Not Supported",
                "The selected command cannot contain child actions.",
            )
            return
        if command.id == "switch_value":
            case = self._make_command("switch_case")
            default_index = next(
                (
                    index
                    for index, branch in enumerate(command.children)
                    if branch.id == "switch_default"
                ),
                len(command.children),
            )
            command.children.insert(default_index, case)
            self._refresh_actions(case.step_id)
            return
        visible = self._visible_action_builtin_types(command.step_id)
        self._add_to(
            command.children,
            base_builtin_types=extend_builtin_types(
                visible,
                definition.get("childBuiltins", ()),
            ),
        )

    def _add_else_action(self) -> None:
        command = self._selected_action()
        if command is None:
            QMessageBox.information(
                self,
                "Select a Parent",
                "Select a conditional command before adding an else action.",
            )
            return
        definition = self._command_types.get(command.id, {})
        if not definition.get("canHaveElse", False):
            QMessageBox.information(
                self,
                "Else Not Supported",
                "The selected command does not support an else branch.",
            )
            return
        self._add_to(
            command.else_children,
            base_builtin_types=self._visible_action_builtin_types(
                command.step_id
            ),
        )

    def _add_to(
        self,
        destination: list[FlowCommand],
        *,
        base_builtin_types: dict[str, Any],
    ) -> None:
        candidates = self._action_candidates()
        dialog = CommandPaletteDialog(candidates, self)
        if not dialog.exec() or not dialog.get_selected():
            return
        command_id = dialog.get_selected()
        command = self._make_command(command_id)
        edit = _CommandEditDialog(
            command,
            self._command_types,
            self._flow,
            self._db,
            self,
            action_roots=[command],
            base_builtin_types=base_builtin_types,
        )
        if edit.exec() == QDialog.DialogCode.Accepted:
            destination.append(command)
            self._refresh_actions(command.step_id)

    def _make_command(self, command_id: str) -> FlowCommand:
        command = FlowCommand(
            command_id,
            params=get_default_params(self._command_types[command_id]),
        )
        if command_id == "switch_value":
            command.children = [
                self._make_command("switch_case"),
                self._make_command("switch_default"),
            ]
        return command

    def _edit_action(self) -> None:
        selected = self._selected_action()
        location = self._find_action(selected.step_id) if selected else None
        if location is None:
            return
        siblings, index = location
        candidate = copy.deepcopy(siblings[index])
        dialog = _CommandEditDialog(
            candidate,
            self._command_types,
            self._flow,
            self._db,
            self,
            action_roots=self._actions,
            base_builtin_types=self._hook_action_builtin_types(),
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            siblings[index] = candidate
            self._refresh_actions(candidate.step_id)

    def _hook_action_builtin_types(self) -> dict[str, Any]:
        if (
            self._type is HookType.VISUAL_IMAGE
            and HookMatch(self.match_combo.currentData()) is HookMatch.FOUND
        ):
            return dict(IMAGE_MATCH_BUILTIN_TYPES)
        return {}

    def _visible_action_builtin_types(self, step_id: str) -> dict[str, Any]:
        return builtin_types_for_step(
            self._actions,
            step_id,
            self._command_types,
            inherited=self._hook_action_builtin_types(),
        )

    def _remove_action(self) -> None:
        selected = self._selected_action()
        location = self._find_action(selected.step_id) if selected else None
        if location is not None:
            siblings, index = location
            if selected.id == "switch_default":
                QMessageBox.information(
                    self,
                    "Default Branch Required",
                    "A Switch command must retain its Default branch.",
                )
                return
            if selected.id == "switch_case" and sum(
                sibling.id == "switch_case" for sibling in siblings
            ) <= 1:
                QMessageBox.information(
                    self,
                    "Case Required",
                    "A Switch command must retain at least one Case branch.",
                )
                return
            siblings.pop(index)
            fallback = siblings[min(index, len(siblings) - 1)].step_id if siblings else None
            self._refresh_actions(fallback)

    def _move_action(self, offset: int) -> None:
        selected = self._selected_action()
        location = self._find_action(selected.step_id) if selected else None
        if location is None:
            return
        siblings, index = location
        target = index + offset
        if not 0 <= target < len(siblings):
            return
        if {selected.id, siblings[target].id} & {"switch_default"}:
            return
        siblings[index], siblings[target] = siblings[target], siblings[index]
        self._refresh_actions(selected.step_id)

    def _selected_action(self) -> FlowCommand | None:
        item = self.actions_tree.currentItem()
        if item is None:
            return None
        location = self._find_action(
            str(item.data(0, Qt.ItemDataRole.UserRole))
        )
        return location[0][location[1]] if location else None

    def _find_action(
        self,
        step_id: str,
        commands: list[FlowCommand] | None = None,
    ) -> tuple[list[FlowCommand], int] | None:
        commands = self._actions if commands is None else commands
        for index, command in enumerate(commands):
            if command.step_id == step_id:
                return commands, index
            for branch in (command.children, command.else_children):
                found = self._find_action(step_id, branch)
                if found is not None:
                    return found
        return None

    def _refresh_actions(self, selected_step_id: str | None = None) -> None:
        self.actions_tree.clear()
        selected_item: QTreeWidgetItem | None = None

        def add_commands(
            commands: list[FlowCommand],
            parent: QTreeWidgetItem | None,
            branch: str,
        ) -> None:
            nonlocal selected_item
            for command in commands:
                definition = self._command_types.get(command.id, {})
                item = QTreeWidgetItem(
                    [definition.get("label", command.id), branch]
                )
                item.setData(0, Qt.ItemDataRole.UserRole, command.step_id)
                if parent is None:
                    self.actions_tree.addTopLevelItem(item)
                else:
                    parent.addChild(item)
                if command.step_id == selected_step_id:
                    selected_item = item
                add_commands(command.children, item, "Children")
                add_commands(command.else_children, item, "Else")

        add_commands(self._actions, None, "Root")
        self.actions_tree.expandAll()
        if selected_item is not None:
            self.actions_tree.setCurrentItem(selected_item)

    def _accept_valid(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Hook Name Required", "Enter a name for this hook.")
            return
        if any(
            item is not self._original and item.name.strip().casefold() == name.casefold()
            for item in self._flow.hooks
        ):
            QMessageBox.warning(self, "Hook Name Taken", "Hook names must be unique in this flow.")
            return
        if not self.before_check.isChecked() and not self.after_check.isChecked():
            QMessageBox.warning(self, "Phase Required", "Select Before action, After action, or both.")
            return
        try:
            condition = self._condition()
            if (
                not (
                    isinstance(condition, VisualImageHookCondition)
                    and condition.match is HookMatch.FOUND
                )
                and self._actions_reference_match_builtins(self._actions)
            ):
                QMessageBox.warning(
                    self,
                    "Match Coordinates Are Unavailable",
                    "_match_x, _match_y, _match_confidence, and _matched_image "
                    "are available only when a Visual Image hook matches "
                    "'Condition is found / visible'. Remove those references "
                    "or change the condition.",
                )
                return
            hook_kwargs: dict[str, Any] = {}
            if self._original is not None:
                hook_kwargs["hook_id"] = self._original.hook_id
            self._result = AutomationHook(
                name=name,
                description=self.description_edit.text().strip(),
                enabled=self.enabled_check.isChecked(),
                condition=condition,
                actions=self._actions,
                automatic_scope=HookAutomaticScope(self.scope_combo.currentData()),
                automatic_phases=tuple(
                    phase
                    for phase, checked in (
                        (HookPhase.BEFORE, self.before_check.isChecked()),
                        (HookPhase.AFTER, self.after_check.isChecked()),
                    )
                    if checked
                ),
                retrigger=HookRetriggerPolicy(self.retrigger_combo.currentData()),
                **hook_kwargs,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Hook", str(exc))
            return
        self.accept()

    @classmethod
    def _actions_reference_match_builtins(
        cls,
        commands: list[FlowCommand],
    ) -> bool:
        names = set(IMAGE_MATCH_BUILTIN_TYPES)

        def contains(value: object) -> bool:
            if isinstance(value, dict):
                if value.get("kind") == "builtin" and value.get("name") in names:
                    return True
                return any(contains(item) for item in value.values())
            if isinstance(value, (list, tuple)):
                return any(contains(item) for item in value)
            return False

        return any(
            contains(command.params)
            or cls._actions_reference_match_builtins(command.children)
            or cls._actions_reference_match_builtins(command.else_children)
            for command in commands
        )

    def hook(self) -> AutomationHook:
        return self._result


class HooksManagerDialog(QDialog):
    def __init__(
        self,
        flow: AutomationFlow,
        command_types: dict[str, dict[str, Any]],
        db: Any,
        parent: QWidget | None = None,
        *,
        test_condition: Callable[[AutomationHook], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Flow Hooks")
        self.resize(720, 560)
        self._flow = flow
        self._types = command_types
        self._db = db
        self._test_condition = test_condition
        self._hooks = [AutomationHook.from_dict(item.to_dict()) for item in flow.hooks]
        self._removed_hook_ids: set[str] = set()
        root = QVBoxLayout(self)
        root.addWidget(QLabel("Hooks run in this order. Conditions are immediate, side-effect-free probes."))
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _item: self._edit())
        root.addWidget(self.list, 1)
        row = QHBoxLayout()
        for label, callback in (
            ("New Image Hook…", lambda: self._add(HookType.VISUAL_IMAGE)),
            ("New Browser Element Hook…", lambda: self._add(HookType.BROWSER_ELEMENT)),
            ("Edit", self._edit),
            ("Duplicate", self._duplicate),
            ("Delete", self._delete),
            ("Up", lambda: self._move(-1)),
            ("Down", lambda: self._move(1)),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            row.addWidget(button)
        root.addLayout(row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._refresh()

    def _refresh(self, selected: int = 0) -> None:
        self.list.clear()
        for hook in self._hooks:
            state = "" if hook.enabled else " · disabled"
            self.list.addItem(f"{hook.name} · {hook.type.value.replace('_', ' ')} · {hook.automatic_scope.value.replace('_', ' ')}{state}")
        if self._hooks:
            self.list.setCurrentRow(max(0, min(selected, len(self._hooks) - 1)))

    def _add(self, hook_type: HookType) -> None:
        shadow = copy.copy(self._flow)
        shadow.hooks = self._hooks
        dialog = HookEditDialog(
            shadow,
            self._types,
            self._db,
            hook_type=hook_type,
            test_condition=self._test_condition,
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._hooks.append(dialog.hook())
            self._refresh(len(self._hooks) - 1)

    def _edit(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        shadow = copy.copy(self._flow)
        shadow.hooks = self._hooks
        dialog = HookEditDialog(
            shadow,
            self._types,
            self._db,
            hook=self._hooks[row],
            test_condition=self._test_condition,
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._hooks[row] = dialog.hook()
            self._refresh(row)

    def _duplicate(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        copied = self._hooks[row].deep_copy()
        copied.name = f"{copied.name} copy"
        self._hooks.insert(row + 1, copied)
        self._refresh(row + 1)

    def _delete(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            candidate = self._hooks[row]
            usages: list[str] = []

            def collect(commands: list[FlowCommand]) -> None:
                for command in commands:
                    if any(
                        binding.hook_id == candidate.hook_id
                        for binding in command.hook_bindings
                    ) or (
                        command.id == "check_hooks"
                        and candidate.hook_id in command.params.get("hooks", [])
                    ):
                        usages.append(f"{command.id} [{command.short_id}]")
                    collect(command.children)
                    collect(command.else_children)

            collect(self._flow.commands)
            if usages:
                answer = QMessageBox.question(
                    self,
                    "Hook Is In Use",
                    "This hook is referenced by:\n"
                    + "\n".join(usages)
                    + "\n\nRemove those references and delete the hook?",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            removed = self._hooks.pop(row)
            self._removed_hook_ids.add(removed.hook_id)
            self._refresh(min(row, len(self._hooks) - 1))

    def _move(self, offset: int) -> None:
        row = self.list.currentRow()
        target = row + offset
        if row >= 0 and 0 <= target < len(self._hooks):
            self._hooks[row], self._hooks[target] = self._hooks[target], self._hooks[row]
            self._refresh(target)

    def hooks(self) -> list[AutomationHook]:
        return self._hooks

    def removed_hook_ids(self) -> set[str]:
        return set(self._removed_hook_ids)


__all__ = ["HookEditDialog", "HooksManagerDialog"]
