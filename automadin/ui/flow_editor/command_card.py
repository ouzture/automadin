"""Command card widget representing a single FlowCommand in the editor."""

from __future__ import annotations

from collections.abc import Mapping
from html import escape
from typing import Any

from PySide6.QtCore import QMimeData, QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QDrag, QPainter, QColor, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from automadin.models.flow import CURRENT_FLOW_VALUE_MODEL_VERSION, FlowCommand
from automadin.models.hook import (
    HookAutomaticScope,
    HookBinding,
    HookPhase,
    HookType,
)
from automadin.models.hook_compatibility import hook_points_for_command
from automadin.models.builtin_scope import builtin_types_for_step
from automadin.models.value_reference import (
    FlowValueTemplate,
    ValueReferenceError,
    render_value_reference,
    render_value_template,
    resolve_value_reference_type,
    value_reference_from_dict,
)
from automadin.models.typed_value import ValueType
from automadin.ui.flow_editor.param_editor import NoScrollComboBox, NoScrollSpinBox, ParamEditorWidget
from automadin.ui.flow_editor.command_presentation import (
    CommandPresentation,
    FieldPresentation,
    PresentationRow,
    PresentationSection,
    command_presentation,
    field_is_visible,
    literal_parameter_value,
    presentation_visibility_parameters,
    row_is_visible,
    section_is_visible,
)
from automadin.ui.flow_editor.run_flow_binding_widget import RunFlowBindingWidget
from automadin.ui.flow_editor.variable_update_fields import (
    configure_variable_update_param_defs,
    normalize_variable_update_editor_change,
)
from automadin.ui.flow_editor.value_condition_fields import (
    configure_value_condition_param_defs,
    normalize_value_condition_editor_change,
)
from automadin.ui.theme import (
    CATEGORY_BG_TINTS,
    CATEGORY_COLORS,
    COLORS,
    tint_color,
)

MIME_COMMAND_STEP_ID = "application/x-automadin-step-id"

# Drop zone detection — which region of the card the cursor is in
DROP_ZONE_ABOVE = "above"       # top 20% → insert before this card
DROP_ZONE_BELOW = "below"       # bottom 20% → insert after this card
DROP_ZONE_INTO = "into"         # middle 60% → add as child (only canHaveChildren)
DROP_ZONE_INTO_ELSE = "into_else"  # into else branch


class BranchDropBody(QWidget):
    """Branch body that accepts drops into a specific child list."""

    def __init__(
        self,
        card: CommandCard,
        branch_attr: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._card = card
        self._branch_attr = branch_attr
        self._show_drop = False
        self.setAcceptDrops(True)
        self.setMinimumHeight(34)
        self.setStyleSheet(
            f"QWidget {{ border-left: 2px solid {COLORS['border']}; }}"
        )

    def dragEnterEvent(self, event: Any) -> None:
        if self._can_accept(event):
            self._show_drop = True
            self.update()
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event: Any) -> None:
        if self._can_accept(event):
            if hasattr(self._card._editor, "_auto_scroll_during_drag"):
                self._card._editor._auto_scroll_during_drag()
            self._show_drop = True
            self.update()
            event.acceptProposedAction()
            return
        event.ignore()

    def dragLeaveEvent(self, event: Any) -> None:
        self._show_drop = False
        self.update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: Any) -> None:
        self._show_drop = False
        self.update()
        if not self._can_accept(event):
            event.ignore()
            return
        step_id = bytes(event.mimeData().data(MIME_COMMAND_STEP_ID)).decode("utf-8")
        self._card._editor._move_command_to_branch(
            step_id,
            self._card._command,
            self._branch_attr,
        )
        event.acceptProposedAction()

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        if not self._show_drop:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(COLORS["accent"]), 2))
        painter.setBrush(QColor(88, 166, 255, 24))
        painter.drawRoundedRect(4, 2, self.width() - 8, self.height() - 4, 5, 5)
        painter.end()

    def _can_accept(self, event: Any) -> bool:
        if not event.mimeData().hasFormat(MIME_COMMAND_STEP_ID):
            return False
        step_id = bytes(event.mimeData().data(MIME_COMMAND_STEP_ID)).decode("utf-8")
        if step_id == self._card._command.step_id:
            return False
        moved = self._card._editor._find_by_step_id(
            self._card._editor._flow.commands,
            step_id,
        )
        if moved is None:
            return False
        if moved.id in {"switch_case", "switch_default"}:
            return False
        return not self._card._editor._is_descendant_of(moved, self._card._command)


class CommandCard(QFrame):
    """Visual card for a single FlowCommand with editing controls."""

    def __init__(
        self,
        command: FlowCommand,
        command_types: dict[str, dict[str, Any]],
        parent_editor: Any,
        depth: int = 0,
        index: int = 0,
        db: Any | None = None,
        parent: QWidget | None = None,
        *,
        parameter_only: bool = False,
        presentation_override: CommandPresentation | None = None,
    ) -> None:
        super().__init__(parent)
        self._command = command
        self._command_types = command_types
        self._editor = parent_editor
        self._depth = depth
        self._index = index
        self._db = db
        self._parameter_only = parameter_only
        self._presentation_override = presentation_override
        self._drop_zone: str | None = None  # current drop zone for painting
        self._drag_start_pos: QPoint | None = None
        self._outline_selected = False
        self._run_step_controls_locked = False
        self._run_step_control_states: list[tuple[QWidget | QAction, bool]] = []
        self._lane_add_buttons: list[QPushButton] = []
        self._error_embedded = False
        self._params_rebuild_timer = QTimer(self)
        self._params_rebuild_timer.setSingleShot(True)
        self._params_rebuild_timer.timeout.connect(self._rebuild_params)

        cmd_def = command_types.get(command.id, {})
        category = cmd_def.get("category", "ACTIONS")
        self._category = category
        self._label_text = cmd_def.get("label", self._command.id)
        self._category_color = CATEGORY_COLORS.get(category, COLORS["primary"])
        self._category_bg = CATEGORY_BG_TINTS.get(category, COLORS["bg_surface"])
        self._strip_color = self._category_color
        self._can_have_children = cmd_def.get("canHaveChildren", False)
        self._can_have_else = cmd_def.get("canHaveElse", False)
        self._collapse_children = cmd_def.get("collapseChildren", False)
        self._is_switch_branch = command.id in {"switch_case", "switch_default"}
        if hasattr(parent_editor, "_register_command_card"):
            parent_editor._register_command_card(command.step_id, self)
        self._collapsed = command.collapsed

        self.setObjectName("CommandCard")
        self.setAcceptDrops(True)
        self._apply_default_style()
        left_margin = depth * 20
        self.setContentsMargins(left_margin, 0, 0, 0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(0, 0, 0, 0)
        self._main_layout.setSpacing(0)

        if self._parameter_only:
            self._build_params(cmd_def)
            return

        if self._is_switch_branch:
            self._build_switch_branch_header()
            if self._command.id == "switch_case":
                self._build_switch_case_explanation()
            self._build_params(cmd_def)
            self._build_children_section()
        else:
            self._build_header(cmd_def)
            self._build_comment()
            self._build_collapsed_info()
            self._build_params(cmd_def)
            self._build_hooks_section(cmd_def)
            if not self._error_embedded:
                self._build_error_section()
            if self._can_have_children:
                if self._command.id == "switch_value":
                    self._build_switch_branches_section()
                else:
                    self._build_children_section()
            if self._can_have_else:
                self._build_else_section()

        if self._collapsed:
            self._apply_collapsed_visibility()
        controller = (
            None
            if self._is_switch_branch
            else getattr(self._editor, "_run_step_controller", None)
        )
        if controller is not None and controller.is_busy:
            self._on_run_step_state_changed(
                controller.active_step_id or "",
                controller.state,
            )

    def _build_switch_branch_header(self) -> None:
        """Build a structural header using the standard command-card language."""
        header = QWidget()
        header.setObjectName("switchBranchHeader")
        header.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(10, 5, 8, 5)
        layout.setSpacing(6)

        if self._command.id == "switch_case":
            drag_handle = QLabel("\u2630")
            drag_handle.setFixedWidth(16)
            drag_handle.setCursor(Qt.CursorShape.OpenHandCursor)
            drag_handle.setToolTip("Drag to reorder Case")
            drag_handle.setStyleSheet(
                f"color: {COLORS['text_tertiary']}; font-size: 11.25pt; "
                "padding: 0; background: transparent;"
            )
            drag_handle.mousePressEvent = self._drag_handle_pressed
            drag_handle.mouseMoveEvent = self._drag_handle_moved
            self._drag_handle = drag_handle
            layout.addWidget(drag_handle)
            title = f"Case {self._index + 1}"
        else:
            title = "Default"

        label = QLabel(title)
        label.setObjectName("switchBranchLabel")
        label.setStyleSheet(
            f"color: {COLORS['text_on_accent']}; background-color: {self._strip_color}; "
            "font-size: 10pt; font-weight: 750; border-radius: 4px; "
            "padding: 3px 8px;"
        )
        layout.addWidget(label)

        if self._command.id == "switch_case":
            self._switch_case_condition_label = QLabel()
            self._switch_case_condition_label.setObjectName(
                "switchCaseConditionLabel"
            )
            self._switch_case_condition_label.setTextFormat(Qt.TextFormat.PlainText)
            self._switch_case_condition_label.setStyleSheet(
                f"color: {COLORS['text_secondary']}; font-size: 10pt;"
            )
            layout.addWidget(self._switch_case_condition_label)

            self._switch_case_explanation_label = QLabel()
            self._switch_case_explanation_label.setObjectName(
                "switchCaseExplanationLabel"
            )
            self._switch_case_explanation_label.setTextFormat(
                Qt.TextFormat.PlainText
            )
            self._switch_case_explanation_label.setStyleSheet(
                f"color: {COLORS['text_tertiary']}; font-size: 10pt; "
                "font-style: italic;"
            )
            layout.addWidget(self._switch_case_explanation_label)
            self._update_switch_case_header_summary()
        layout.addStretch()

        if self._command.id == "switch_case":
            parent_switch = self._editor._find_parent_command(
                self._editor._flow.commands,
                self._command,
            )
            cases = (
                [
                    branch
                    for branch in parent_switch.children
                    if branch.id == "switch_case"
                ]
                if parent_switch is not None
                else []
            )
            case_position = cases.index(self._command) if self._command in cases else -1

            actions_menu = QMenu(self)

            duplicate_action = QAction("Duplicate", self)
            duplicate_action.triggered.connect(
                lambda: self._editor._duplicate_switch_case(self._command)
            )
            actions_menu.addAction(duplicate_action)

            up_action = QAction("Move Up", self)
            up_action.setEnabled(case_position > 0)
            up_action.triggered.connect(
                lambda: self._editor._move_switch_case(self._command, -1)
            )
            actions_menu.addAction(up_action)

            down_action = QAction("Move Down", self)
            down_action.setEnabled(0 <= case_position < len(cases) - 1)
            down_action.triggered.connect(
                lambda: self._editor._move_switch_case(self._command, 1)
            )
            actions_menu.addAction(down_action)

            actions_menu.addSeparator()
            remove_action = QAction("Remove", self)
            remove_action.setEnabled(len(cases) > 1)
            remove_action.triggered.connect(
                lambda: self._editor._remove_switch_case(self._command)
            )
            actions_menu.addAction(remove_action)

            actions_btn = QPushButton("...")
            actions_btn.setObjectName("switchCaseActionsButton")
            actions_btn.setFixedWidth(34)
            actions_btn.setToolTip("Case actions")
            actions_btn.setAccessibleName(f"Case {case_position + 1} actions")
            actions_btn.setMenu(actions_menu)
            layout.addWidget(actions_btn)

        expand_btn = QPushButton("\u25be" if self._collapsed else "\u25b4")
        expand_btn.setFixedSize(30, 30)
        expand_btn.clicked.connect(self._toggle_collapsed)
        self._expand_btn = expand_btn
        self._update_expand_button_accessibility()
        layout.addWidget(expand_btn)
        header.mousePressEvent = self._header_clicked
        self._main_layout.addWidget(header)

    def _switch_case_condition_summary(self) -> str:
        """Return the readable comparison shown in a Case header."""
        command_definition = self._command_types.get("switch_case", {})
        operator_definition = next(
            (
                definition
                for definition in command_definition.get("params", [])
                if definition.get("name") == "operator"
            ),
            {},
        )
        operator = str(self._command.params.get("operator", "equals"))
        operator_label = operator_definition.get("option_labels", {}).get(
            operator,
            operator.replace("_", " ").capitalize(),
        )
        right = self._command.params.get("right")
        if right is None:
            summary = str(operator_label)
            if operator not in {"is_empty", "is_not_empty"}:
                summary += " — value not set"
        else:
            summary = f"{operator_label} {self._format_summary_value('right', right)}"
        if self._command.params.get("ignore_case"):
            summary += " · ignore case"
        return summary

    def _update_switch_case_header_summary(self) -> None:
        if not hasattr(self, "_switch_case_condition_label"):
            return
        self._switch_case_condition_label.setText(
            self._switch_case_condition_summary()
        )
        explanation = self._command.comment.strip()
        self._switch_case_explanation_label.setText(
            f"— {explanation}" if explanation else ""
        )
        self._switch_case_explanation_label.setVisible(bool(explanation))

    def _build_switch_case_explanation(self) -> None:
        """Add the persisted optional explanation editor for a Case."""
        self._comment_edit = QLineEdit(self._command.comment)
        self._comment_edit.setObjectName("switchCaseExplanationEdit")
        self._comment_edit.setPlaceholderText("Explain this case (optional)…")
        self._comment_edit.setToolTip(
            "This explanation is shown in the Case header when filled."
        )
        self._comment_edit.setStyleSheet(
            f"QLineEdit {{ font-style: italic; color: {COLORS['text_secondary']}; "
            f"border: none; border-bottom: 1px solid {COLORS['border_light']}; "
            "border-radius: 0; padding: 4px 12px; background: transparent; }"
        )
        self._comment_edit.textChanged.connect(self._on_comment_changed)
        self._main_layout.addWidget(self._comment_edit)

    def _apply_default_style(self) -> None:
        if self._command.id == "switch_case":
            semantic_color = COLORS["primary"]
            base_bg = tint_color(semantic_color, COLORS["bg_surface"], 0.07)
        elif self._command.id == "switch_default":
            semantic_color = COLORS["warning"]
            base_bg = tint_color(semantic_color, COLORS["bg_surface"], 0.055)
        else:
            semantic_color = COLORS["border"]
            base_bg = COLORS["bg_surface"]
        self._strip_color = semantic_color
        strip_color = semantic_color if self._command.enabled else COLORS["border_light"]
        border_color = COLORS["primary"] if self._outline_selected else COLORS["border_light"]
        bg_color = COLORS["bg_elevated"] if self._outline_selected else "transparent"
        if not self._outline_selected:
            bg_color = base_bg
        hover_bg = COLORS["bg_hover"]
        self.setStyleSheet(f"""
            #CommandCard {{
                background-color: {bg_color};
                border: 1px solid {border_color};
                border-left: 1px solid {strip_color};
                border-radius: 5px;
                margin: 1px 0;
            }}
            #CommandCard:hover {{
                background-color: {hover_bg};
                border-color: {COLORS["border_light"]};
            }}
        """)

    def set_outline_selected(self, selected: bool) -> None:
        if self._outline_selected == selected:
            return
        self._outline_selected = selected
        self._apply_default_style()

    def _enabled_button_style(self) -> str:
        return (
            f"QPushButton {{ color: {COLORS['text_secondary']}; font-size: 10pt; "
            f"font-weight: 750; padding: 0; background: {COLORS['border_light']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 4px; }}"
            f"QPushButton:hover {{ color: {COLORS['text_primary']}; border-color: {COLORS['primary']}; }}"
            f"QPushButton:checked {{ color: {COLORS['text_primary']}; "
            f"background: {COLORS['success']}; border-color: {COLORS['success']}; }}"
        )

    def _tag_style(self) -> str:
        color = (
            COLORS["text_on_accent"]
            if self._command.enabled
            else COLORS["text_tertiary"]
        )
        background = self._category_color if self._command.enabled else COLORS["border_light"]
        return (
            f"font-weight: 700; font-size: 10.25pt; color: {color}; "
            f"background-color: {background}; border-radius: 4px; padding: 4px 8px; "
            f"border: 1px solid {background};"
        )

    def _category_label_style(self) -> str:
        color = COLORS["text_tertiary"] if self._command.enabled else COLORS["border"]
        return f"font-size: 10pt; color: {color}; background: transparent;"

    def _summary_label_style(self) -> str:
        color = COLORS["text_secondary"] if self._command.enabled else COLORS["text_tertiary"]
        return f"font-size: 10.25pt; color: {color}; background: transparent;"

    # ── Header ──────────────────────────────────────────────────────────

    def _build_header(self, cmd_def: dict[str, Any]) -> None:
        header = QWidget()
        header.setCursor(Qt.CursorShape.PointingHandCursor)
        header.setStyleSheet(
            f"background: transparent; border-left: 4px solid {self._strip_color};"
        )
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(6, 5, 6, 5)
        h_layout.setSpacing(6)
        header.setMinimumHeight(40)

        # Drag handle
        drag_handle = QLabel("\u2630")  # hamburger
        drag_handle.setFixedWidth(16)
        drag_handle.setCursor(Qt.CursorShape.OpenHandCursor)
        drag_handle.setToolTip("Drag to reorder")
        drag_handle.setStyleSheet(
            f"color: {COLORS['text_tertiary']}; font-size: 11.25pt; padding: 0; background: transparent;"
        )
        drag_handle.mousePressEvent = self._drag_handle_pressed
        drag_handle.mouseMoveEvent = self._drag_handle_moved
        self._drag_handle = drag_handle
        h_layout.addWidget(drag_handle)

        step_label = QLabel(f"{self._index + 1:02}")
        step_label.setFixedWidth(24)
        step_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        step_label.setStyleSheet(
            f"font-family: 'Cascadia Code', 'Consolas', monospace; font-size: 10pt; "
            f"color: {COLORS['text_tertiary']}; background: transparent;"
        )
        h_layout.addWidget(step_label)

        enabled_btn = QPushButton("On" if self._command.enabled else "Off")
        enabled_btn.setFixedSize(42, 26)
        enabled_btn.setCheckable(True)
        enabled_btn.setChecked(self._command.enabled)
        enabled_btn.setToolTip("Enable/disable this command")
        enabled_btn.setStyleSheet(self._enabled_button_style())
        enabled_btn.toggled.connect(self._on_enabled_toggle)
        self._enabled_btn = enabled_btn
        h_layout.addWidget(enabled_btn)

        label = QLabel(self._label_text)
        label.setMinimumWidth(104)
        label.setMaximumWidth(164)
        label.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        label.setStyleSheet(self._tag_style())
        self._action_label = label
        h_layout.addWidget(label)

        self._summary_label = QLabel()
        self._summary_label.setTextFormat(Qt.TextFormat.RichText)
        self._summary_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._summary_label.setStyleSheet(self._summary_label_style())
        h_layout.addWidget(self._summary_label, 1)
        self._update_summary()

        _hdr_btn = (
            f"QPushButton {{ color: {COLORS['text_secondary']}; font-size: 13.5pt; "
            f"font-weight: bold; padding: 0; background: transparent; border: none; border-radius: 4px; }}"
            f"QPushButton:hover {{ color: {COLORS['text_primary']}; background: {COLORS['bg_elevated']}; }}"
            f"QPushButton:checked {{ color: {COLORS['warning']}; "
            f"background: {tint_color(COLORS['warning'], COLORS['bg_elevated'], 0.18)}; }}"
        )

        _run_btn = (
            f"QPushButton {{ color: {COLORS['success']}; font-size: 13.5pt; font-weight: bold; "
            f"padding: 0; background: transparent; border: none; border-radius: 4px; }}"
            f"QPushButton:hover {{ color: {COLORS['success_hover']}; "
            f"background: {tint_color(COLORS['success'], COLORS['bg_elevated'], 0.16)}; }}"
        )
        _cancel_run_btn = (
            f"QPushButton {{ color: {COLORS['warning']}; font-size: 13.5pt; "
            "font-weight: bold; padding: 0; background: transparent; "
            f"border: 1px solid {COLORS['warning']}; border-radius: 4px; }}"
            f"QPushButton:hover {{ color: {COLORS['text_on_accent']}; "
            f"background: {COLORS['warning']}; }}"
        )

        expand_btn = QPushButton("\u25be" if self._collapsed else "\u25b4")
        expand_btn.setFixedSize(30, 30)
        expand_btn.setStyleSheet(_hdr_btn)
        expand_btn.clicked.connect(self._toggle_collapsed)
        self._expand_btn = expand_btn
        self._update_expand_button_accessibility()
        h_layout.addWidget(expand_btn)

        # Run Step button
        run_btn = QPushButton("\u25b6")
        run_btn.setFixedSize(34, 32)
        run_btn.setToolTip("Run this step" if self._command.enabled else "Command is disabled")
        run_btn.setAccessibleName(f"Run step: {self._label_text}")
        run_btn.setAccessibleDescription(
            "Runs this command against the currently connected target."
        )
        run_btn.setStyleSheet(_run_btn)
        run_btn.setEnabled(self._command.enabled)
        run_btn.clicked.connect(self._on_run_step)
        self._run_btn = run_btn
        self._run_btn_style = _run_btn
        self._cancel_run_btn_style = _cancel_run_btn
        h_layout.addWidget(run_btn)
        if hasattr(self._editor, "run_step_state_changed"):
            self._editor.run_step_state_changed.connect(
                self._on_run_step_state_changed
            )

        preview_handler = (
            "preview_screen_text"
            if self._command.id == "read_screen_text"
            else "preview_dynamic_target"
            if self._command.id == "click_dynamic_target"
            else "preview_image_search"
            if self._command.id
            in {
                "click_image", "find_image", "if_image", "while",
                "win_click_image", "win_drag_image", "win_if_image", "win_while",
            }
            else None
        )
        if preview_handler is not None and hasattr(self._editor, preview_handler):
            preview_btn = QPushButton("Preview")
            preview_btn.setToolTip("Capture and preview this command without running it")
            preview_btn.setAccessibleName(f"Preview command: {self._label_text}")
            preview_btn.clicked.connect(
                lambda: getattr(self._editor, preview_handler)(self._command)
            )
            self._preview_btn = preview_btn
            h_layout.addWidget(preview_btn)

        insert_btn = QPushButton("+")
        insert_btn.setFixedSize(30, 30)
        insert_btn.setToolTip("Insert above")
        insert_btn.setAccessibleName(f"Insert command above: {self._label_text}")
        insert_btn.setAccessibleDescription(
            "Adds a new command immediately before this command."
        )
        insert_btn.setStyleSheet(_hdr_btn)
        insert_btn.clicked.connect(self._on_insert_above)
        self._insert_btn = insert_btn
        h_layout.addWidget(insert_btn)

        more_btn = QPushButton("...")
        more_btn.setFixedSize(34, 30)
        more_btn.setToolTip("Command actions")
        more_btn.setAccessibleName(f"Command actions: {self._label_text}")
        more_btn.setAccessibleDescription(
            "Opens actions for editing, moving, or deleting this command."
        )
        more_btn.setStyleSheet(_hdr_btn)
        self._actions_menu = self._build_actions_menu()
        more_btn.setMenu(self._actions_menu)
        self._more_btn = more_btn
        h_layout.addWidget(more_btn)

        header.mousePressEvent = self._header_clicked
        self._main_layout.addWidget(header)

    def _build_actions_menu(self) -> QMenu:
        menu = QMenu(self)

        duplicate_action = QAction("Duplicate", self)
        duplicate_action.triggered.connect(self._on_duplicate)
        menu.addAction(duplicate_action)

        insert_action = QAction("Insert Above", self)
        insert_action.triggered.connect(self._on_insert_above)
        menu.addAction(insert_action)

        move_action = QAction("Move To…", self)
        move_action.triggered.connect(self._on_move_to)
        menu.addAction(move_action)

        move_up_action = QAction("Move Up", self)
        move_up_action.triggered.connect(
            lambda: self._editor._move_command(self._command, -1)
        )
        menu.addAction(move_up_action)

        move_down_action = QAction("Move Down", self)
        move_down_action.triggered.connect(
            lambda: self._editor._move_command(self._command, 1)
        )
        menu.addAction(move_down_action)

        breakpoint_action = QAction("Breakpoint", self)
        breakpoint_action.setCheckable(True)
        breakpoint_action.setChecked(self._command.breakpoint)
        breakpoint_action.toggled.connect(self._on_breakpoint_toggle)
        menu.addAction(breakpoint_action)

        enabled_action = QAction("Enabled", self)
        enabled_action.setCheckable(True)
        enabled_action.setChecked(self._command.enabled)
        enabled_action.toggled.connect(self._on_enabled_toggle)
        menu.addAction(enabled_action)

        if self._depth > 0:
            out_action = QAction("Move Out Of Parent", self)
            out_action.triggered.connect(
                lambda: self._editor._move_out_of_parent(self._command)
            )
            menu.addAction(out_action)

        menu.addSeparator()
        delete_action = QAction("Delete", self)
        delete_action.triggered.connect(self._on_delete)
        menu.addAction(delete_action)
        return menu

    def _summary_parts(self, limit: int = 3) -> list[str]:
        param_defs = self._command_types.get(self._command.id, {}).get("params", [])
        parts: list[str] = []
        structure: list[str] = []
        child_count = len(self._command.children)
        else_count = len(self._command.else_children)
        if self._command.id == "switch_value":
            case_count = sum(
                branch.id == "switch_case" for branch in self._command.children
            )
            structure.append(f"{case_count} case{'s' if case_count != 1 else ''}")
            has_default = any(
                branch.id == "switch_default" for branch in self._command.children
            )
            structure.append("default" if has_default else "no default")
        elif self._is_switch_branch:
            structure.append(
                f"{child_count} command{'s' if child_count != 1 else ''}"
            )
        elif child_count:
            structure.append(f"{child_count} then step{'s' if child_count != 1 else ''}")
        if else_count:
            structure.append(f"{else_count} else step{'s' if else_count != 1 else ''}")
        param_limit = max(1, limit - len(structure))
        priority = {
            "url": 0,
            "flow_name": 0,
            "images": 0,
            "image_name": 0,
            "text": 0,
            "script": 0,
            "values": 0,
            "keys": 0,
            "x": 1,
            "y": 1,
            "start_x": 1,
            "start_y": 1,
            "end_x": 1,
            "end_y": 1,
            "ms": 1,
            "count": 1,
            "threshold": 2,
            "condition": 2,
            "click": 2,
            "mode": 2,
            "timeout": 2,
        }
        ordered_defs = sorted(
            param_defs,
            key=lambda p: (
                priority.get(p.get("name", ""), 5),
                1 if p.get("optional") else 0,
            ),
        )
        for pdef in ordered_defs:
            name = pdef["name"]
            if self._command.id == "extract_text":
                target = self._command.params.get("target", "locator")
                if target == "current_element" and name == "locator":
                    continue
                if target != "current_element" and name == "child_selector":
                    continue
            value = self._command.params.get(name)
            if value is None:
                for alias in pdef.get("aliases", []):
                    if alias in self._command.params:
                        value = self._command.params[alias]
                        break
            if self._is_empty_summary_value(value):
                continue
            label = str(pdef.get("label", name)).replace(" (ms)", "")
            parts.append(f"{label}: {self._format_summary_value(name, value)}")
            if len(parts) >= param_limit:
                break
        parts.extend(structure)
        hook_names = self._active_hook_names()
        if hook_names and len(parts) < limit:
            parts.append("hooks: " + ", ".join(hook_names))
        if self._command.comment and len(parts) < limit:
            parts.append(f"note: {self._command.comment}")
        return parts

    def _active_hook_names(self) -> list[str]:
        definition = self._command_types.get(self._command.id, {})
        supported = {
            point.get("type") for point in hook_points_for_command(definition)
        }
        selected = {binding.hook_id for binding in self._command.hook_bindings}
        return [
            hook.name
            for hook in getattr(self._editor._flow, "hooks", ())
            if hook.type.value in supported
            and (
                hook.automatic_scope
                is HookAutomaticScope.ALL_COMPATIBLE_COMMANDS
                or (
                    hook.automatic_scope
                    is HookAutomaticScope.SELECTED_COMMANDS
                    and hook.hook_id in selected
                )
            )
        ]

    def _build_hooks_section(self, cmd_def: dict[str, Any]) -> None:
        if not getattr(self._editor, "_hooks_enabled", True):
            self._hooks_container = None
            return
        points = hook_points_for_command(cmd_def)
        supported = {point.get("type"): point for point in points}
        hooks = [
            hook
            for hook in getattr(self._editor._flow, "hooks", ())
            if hook.type.value in supported
        ]
        if not supported:
            self._hooks_container = None
            return
        container = QFrame()
        container.setObjectName("commandHooksSection")
        container.setStyleSheet(
            f"#commandHooksSection {{ background-color: {self._category_bg}; "
            f"border-top: 1px solid {COLORS['border']}; }}"
        )
        layout = QVBoxLayout(container)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(5)
        title = QLabel("Hooks")
        title.setStyleSheet(
            f"color: {self._category_color}; font-size: 10pt; font-weight: 700;"
        )
        layout.addWidget(title)
        bindings = {binding.hook_id: binding for binding in self._command.hook_bindings}
        for hook in hooks:
            global_hook = (
                hook.automatic_scope
                is HookAutomaticScope.ALL_COMPATIBLE_COMMANDS
            )
            binding = bindings.get(hook.hook_id)
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            selected = global_hook or binding is not None
            enabled = QCheckBox(hook.name)
            enabled.setChecked(selected)
            enabled.setEnabled(not global_hook)
            enabled.setToolTip(
                "Enforced for every compatible command"
                if global_hook
                else hook.description
            )
            row_layout.addWidget(enabled, 1)
            before = QCheckBox("Before")
            after = QCheckBox("After")
            phases = hook.automatic_phases if global_hook else (
                binding.phases if binding else (HookPhase.BEFORE,)
            )
            before.setChecked(HookPhase.BEFORE in phases)
            after.setChecked(HookPhase.AFTER in phases)
            before.setEnabled(selected and not global_hook)
            after.setEnabled(selected and not global_hook)
            row_layout.addWidget(before)
            row_layout.addWidget(after)

            def commit(
                _checked: bool = False,
                *,
                item=hook,
                select=enabled,
                before_box=before,
                after_box=after,
            ) -> None:
                before_box.setEnabled(select.isChecked())
                after_box.setEnabled(select.isChecked())
                phases = tuple(
                    phase
                    for phase, checked in (
                        (HookPhase.BEFORE, before_box.isChecked()),
                        (HookPhase.AFTER, after_box.isChecked()),
                    )
                    if checked
                )
                if select.isChecked() and not phases:
                    before_box.setChecked(True)
                    return
                self._command.hook_bindings = [
                    existing
                    for existing in self._command.hook_bindings
                    if existing.hook_id != item.hook_id
                ]
                if select.isChecked():
                    if item.automatic_scope is HookAutomaticScope.NONE:
                        item.automatic_scope = HookAutomaticScope.SELECTED_COMMANDS
                    self._command.hook_bindings.append(
                        HookBinding(item.hook_id, phases)
                    )
                self._update_summary()

            enabled.toggled.connect(commit)
            before.toggled.connect(commit)
            after.toggled.connect(commit)
            layout.addWidget(row)
        create = QPushButton("Create and attach new hook…")
        preferred_type = HookType(next(iter(supported)))
        create.clicked.connect(
            lambda: self._editor._create_and_attach_hook(
                self._command,
                preferred_type,
            )
        )
        layout.addWidget(create, 0, Qt.AlignmentFlag.AlignLeft)
        self._hooks_container = container
        self._main_layout.addWidget(container)

    @staticmethod
    def _is_empty_summary_value(value: Any) -> bool:
        if value is None:
            return True
        if value == "":
            return True
        if value == []:
            return True
        if value is False:
            return True
        return False

    def _format_summary_value(self, name: str, value: Any) -> str:
        if isinstance(value, Mapping):
            try:
                if value.get("kind") == "template":
                    rendered = render_value_template(
                        FlowValueTemplate.from_dict(value),
                        self._editor._flow.value_catalog,
                    )
                else:
                    rendered = render_value_reference(
                        value_reference_from_dict(value),
                        self._editor._flow.value_catalog,
                    )
            except ValueReferenceError:
                return "Invalid saved value"
            return self._short_summary_text(rendered, 40)
        if isinstance(value, list):
            if not value:
                return ""
            values = [CommandCard._short_summary_text(str(v), 32) for v in value]
            if len(values) <= 2:
                return ", ".join(values)
            return f"{', '.join(values[:2])} +{len(values) - 2}"
        if isinstance(value, bool):
            return "on" if value else "off"
        if isinstance(value, float):
            return f"{value:.2f}".rstrip("0").rstrip(".")
        text = str(value)
        if name in {"script", "values"}:
            line_count = max(1, len(text.splitlines()))
            return f"{line_count} line{'s' if line_count != 1 else ''}"
        return CommandCard._short_summary_text(text, 40)

    @staticmethod
    def _short_summary_text(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        if "/" in text:
            tail = text.rsplit("/", 1)[-1]
            if len(tail) <= limit - 2:
                return f"…/{tail}"
        return text[: limit - 1] + "…"

    def _update_summary(self) -> None:
        if not hasattr(self, "_summary_label"):
            return
        muted = COLORS["text_secondary"] if self._command.enabled else COLORS["text_tertiary"]
        strong = COLORS["text_primary"] if self._command.enabled else COLORS["text_secondary"]
        chunks = []
        if not self._command.enabled:
            chunks.append(f"<span style='color:{COLORS['text_tertiary']};'>disabled</span>")
        for part in self._summary_parts():
            if ": " in part:
                key, value = part.split(": ", 1)
                chunks.append(
                    f"<span style='color:{muted};'>{escape(key)} "
                    f"<b style='color:{strong}; font-weight:500;'>{escape(value)}</b></span>"
                )
            else:
                chunks.append(f"<span style='color:{muted};'>{escape(part)}</span>")
        self._summary_label.setText("&nbsp;&nbsp;".join(chunks))

    # ── Comment ─────────────────────────────────────────────────────────

    def _build_comment(self) -> None:
        self._comment_edit = QLineEdit(self._command.comment)
        self._comment_edit.setPlaceholderText("Add a note...")
        self._comment_edit.setStyleSheet(
            f"QLineEdit {{ font-style: italic; color: {COLORS['text_secondary']}; "
            f"border: none; border-bottom: 1px solid {COLORS['border_light']}; "
            f"border-radius: 0; padding: 4px 12px; background: transparent; }}"
        )
        self._comment_edit.textChanged.connect(self._on_comment_changed)
        self._main_layout.addWidget(self._comment_edit)

    # ── Collapsed Info ──────────────────────────────────────────────────

    def _build_collapsed_info(self) -> None:
        """Read-only label shown when collapsed: displays note + image names."""
        self._collapsed_info = QLabel()
        self._collapsed_info.setWordWrap(True)
        self._collapsed_info.setStyleSheet(
            f"QLabel {{ font-style: italic; color: {COLORS['text_secondary']}; "
            f"padding: 2px 12px; background: transparent; font-size: 10pt; }}"
        )
        self._collapsed_info.setVisible(False)
        self._main_layout.addWidget(self._collapsed_info)

    def _update_collapsed_info(self) -> None:
        """Refresh the collapsed info label with current note + image list."""
        self._collapsed_info.setText("")
        self._collapsed_info.setVisible(False)

    # ── Parameters ──────────────────────────────────────────────────────

    def _build_params(
        self,
        cmd_def: dict[str, Any],
        *,
        layout_index: int | None = None,
    ) -> None:
        self._params_container = QWidget()
        self._params_container.setStyleSheet(
            f"background-color: {self._category_bg}; border-radius: 0;"
        )
        params_layout = QVBoxLayout(self._params_container)
        params_layout.setContentsMargins(12, 6, 12, 8)
        params_layout.setSpacing(6)

        param_defs = cmd_def.get("params", [])
        is_v1_run_flow = bool(
            self._command.id == "run_flow"
            and self._editor._flow.value_model_version
            == CURRENT_FLOW_VALUE_MODEL_VERSION
        )
        if is_v1_run_flow:
            params_layout.addWidget(
                RunFlowBindingWidget(
                    self._command.params,
                    db=self._db,
                    parent_flow=self._editor._flow,
                    builtin_types=self._visible_builtin_types(),
                    on_change=self._on_run_flow_binding_changed,
                )
            )
            param_defs = [
                definition
                for definition in param_defs
                if definition.get("name") in {"pause_ms", "min_wait_time"}
            ]
        elif (
            self._editor._flow.value_model_version
            == CURRENT_FLOW_VALUE_MODEL_VERSION
        ):
            configured_defs: list[dict[str, Any]] = []
            for raw_definition in param_defs:
                definition = dict(raw_definition)
                original_type = definition.get("type")
                if (
                    original_type in {"string", "multiline"}
                    and definition.get("valueRole") == "output_target"
                ):
                    definition["type"] = "variable_target"
                elif (
                    original_type in {"int", "double", "bool"}
                    and definition.get("supportsValueReference", False)
                ):
                    definition["type"] = "value_reference"
                elif (
                    original_type in {"string", "multiline"}
                    and not definition.get("picker")
                    and definition.get("supportsValueTemplate", True)
                ):
                    definition["type"] = "value_template"
                    definition["template_multiline"] = original_type == "multiline"
                elif original_type in {"string", "multiline"}:
                    definition["disable_legacy_insert"] = True
                configured_defs.append(definition)
            param_defs = configured_defs
        if self._command.id == "update_variable":
            param_defs = configure_variable_update_param_defs(
                param_defs,
                self._command.params,
                self._editor._flow.value_catalog,
            )
        elif self._command.id in {"if_value", "while_value", "switch_case"}:
            if self._command.id == "switch_case":
                allowed_types = self._switch_case_allowed_value_types()
                if allowed_types:
                    param_defs = [
                        {
                            **definition,
                            **(
                                {"allowed_value_types": allowed_types}
                                if definition.get("name") == "right"
                                else {}
                            ),
                        }
                        for definition in param_defs
                    ]
            param_defs = configure_value_condition_param_defs(
                param_defs,
                self._command.params,
            )
            case_note = QLabel("Text comparisons are case-sensitive by default.")
            case_note.setStyleSheet(
                f"color: {COLORS['text_tertiary']}; font-size: 10pt;"
            )
            params_layout.addWidget(case_note)
        presentation = self._presentation_override or command_presentation(
            self._command.id
        )
        if presentation is not None:
            self._build_command_presentation(
                params_layout,
                param_defs,
                presentation,
            )
        else:
            for title, grouped_defs in self._group_param_defs(param_defs):
                params_layout.addWidget(self._build_param_group(title, grouped_defs))

        if not param_defs:
            self._params_container.setVisible(False)

        if layout_index is None:
            self._main_layout.addWidget(self._params_container)
        else:
            self._main_layout.insertWidget(layout_index, self._params_container)

    def _build_command_presentation(
        self,
        parent_layout: QVBoxLayout,
        param_defs: list[dict[str, Any]],
        presentation: CommandPresentation,
    ) -> None:
        definitions = {
            str(definition["name"]): definition for definition in param_defs
        }
        if presentation.layout == "compact":
            for row in presentation.rows:
                widget = self._build_presentation_row(row, definitions)
                if widget is not None:
                    parent_layout.addWidget(widget)
            return

        for section in presentation.sections:
            parent_layout.addWidget(
                self._build_guided_section(section, definitions)
            )

    def _build_guided_section(
        self,
        section: PresentationSection,
        definitions: dict[str, dict[str, Any]],
    ) -> QWidget:
        container = QFrame()
        container.setObjectName(f"guidedSection_{section.key}")
        section_visible = section_is_visible(section, self._command.params)
        container.setProperty("presentationVisible", section_visible)
        if not section_visible:
            container.hide()
        container.setStyleSheet(
            f"QFrame#{container.objectName()} {{ border-top: 1px solid "
            f"{COLORS['border']}; background: transparent; }}"
        )
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 2, 0, 4)
        layout.setSpacing(6)

        heading = QWidget()
        heading_layout = QHBoxLayout(heading)
        heading_layout.setContentsMargins(0, 0, 0, 0)
        heading_layout.setSpacing(8)

        state_getter = getattr(self._editor, "_command_section_expanded", None)
        expanded = (
            bool(
                state_getter(
                    self._command.step_id,
                    section.key,
                    section.default_expanded,
                )
            )
            if callable(state_getter)
            else section.default_expanded
        )
        button = QToolButton()
        button.setObjectName(f"guidedSectionButton_{section.key}")
        button.setCheckable(True)
        button.setChecked(expanded)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        button.setStyleSheet(
            f"QToolButton {{ color: {COLORS['text_primary']}; border: none; "
            "font-size: 10.5pt; font-weight: 700; padding: 5px 0; }"
            f"QToolButton:hover {{ color: {COLORS['primary']}; }}"
        )

        def update_button_text(is_expanded: bool) -> None:
            arrow = "▾" if is_expanded else "▸"
            button.setText(f"{arrow}  {section.label}")
            button.setAccessibleName(
                f"{'Collapse' if is_expanded else 'Expand'} {section.label} section"
            )

        update_button_text(expanded)
        heading_layout.addWidget(button)

        summary = QLabel(self._guided_section_summary(section))
        summary.setObjectName(f"guidedSectionSummary_{section.key}")
        summary.setStyleSheet(
            f"color: {COLORS['text_tertiary']}; font-size: 10pt;"
        )
        heading_layout.addWidget(summary)
        heading_layout.addStretch()
        layout.addWidget(heading)

        body = QWidget()
        body.setObjectName(f"guidedSectionBody_{section.key}")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(16, 0, 0, 4)
        body_layout.setSpacing(7)
        if section.description:
            description = QLabel(section.description)
            description.setObjectName(
                f"guidedSectionDescription_{section.key}"
            )
            description.setWordWrap(True)
            description.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            description.setStyleSheet(
                f"color: {COLORS['text_secondary']}; font-size: 10pt; "
                "padding: 2px 0 4px 0;"
            )
            body_layout.addWidget(description)
        for row in section.rows:
            row_widget = self._build_presentation_row(row, definitions)
            if row_widget is not None:
                body_layout.addWidget(row_widget)
        if section.include_error_policy:
            self._error_embedded = True
            body_layout.addWidget(self._create_error_policy_widget())
        layout.addWidget(body)
        if not expanded:
            body.hide()

        def toggle_section(is_expanded: bool) -> None:
            body.setVisible(is_expanded)
            update_button_text(is_expanded)
            state_setter = getattr(
                self._editor,
                "_set_command_section_expanded",
                None,
            )
            if callable(state_setter):
                state_setter(self._command.step_id, section.key, is_expanded)

        button.toggled.connect(toggle_section)
        return container

    def _guided_section_summary(self, section: PresentationSection) -> str:
        command_id = self._command.id
        if section.key == "capture_area" and command_id == "read_screen_text":
            mode = literal_parameter_value(
                self._command.params.get("region_mode", "fixed")
            )
            return (
                "Image-anchored area" if mode == "image_anchor" else "Fixed area"
            )
        if section.key == "result" and command_id == "read_screen_text":
            result_type = literal_parameter_value(
                self._command.params.get("result_type", "text")
            )
            return {
                "text": "Text output",
                "integer": "Whole number output",
                "number": "Decimal number output",
                "duration_ms": "Duration output",
            }.get(str(result_type), "Configured output")
        if section.key == "condition" and command_id in {
            "if_image",
            "while",
            "win_if_image",
            "win_while",
        }:
            condition = literal_parameter_value(
                self._command.params.get("condition", "found")
            )
            return (
                "Image is not found"
                if condition == "not_found"
                else "Image is found"
            )
        if section.key == "action":
            if command_id == "click_position":
                click_type = str(self._command.params.get("click", "single"))
                return f"{click_type.title()} click"
            if command_id == "win_click":
                click_type = str(self._command.params.get("click", "single")).title()
                button = str(self._command.params.get("button", "left"))
                return f"{click_type} {button} {self._windows_input_mode_summary()} click"
            if command_id == "win_click_image":
                click_type = str(self._command.params.get("click", "single")).title()
                button = str(self._command.params.get("button", "left")).title()
                mode = self._windows_input_mode_summary()
                return f"{click_type} {button.lower()} {mode} click"
            if command_id == "win_drag_image":
                button = str(self._command.params.get("button", "left")).title()
                return f"{button}-button {self._windows_input_mode_summary()} drag"
            if command_id in {"if_image", "win_if_image", "win_while"}:
                parameter = (
                    "clickIfFound" if command_id == "if_image" else "click_if_found"
                )
                click = literal_parameter_value(
                    self._command.params.get(parameter, False)
                )
                if click is not True:
                    return "No click"
                if command_id == "win_while":
                    condition = literal_parameter_value(
                        self._command.params.get("condition", "found")
                    )
                    return (
                        "Click when image appears"
                        if condition == "not_found"
                        else "Click each found match"
                    )
                return "Click when found"
        if section.key == "scope":
            mode = literal_parameter_value(
                self._command.params.get("search_region_mode", "full")
            )
            return {
                "full": "Full screen",
                "fixed": "Fixed region",
                "image_anchor": "Region anchored to an image",
            }.get(str(mode), section.summary)
        if section.key == "timing":
            timeout = literal_parameter_value(
                self._command.params.get("timeout", 0)
            )
            if command_id in {"if_image", "win_if_image"}:
                return (
                    f"Wait up to {self._format_duration(timeout)}"
                    if self._positive_number(timeout)
                    else "Check once"
                )
            if command_id == "while":
                return f"Stop after {self._format_duration(timeout)}"
            if command_id == "while_value":
                return f"Stop after {self._format_duration(timeout)}"
            if command_id == "win_while":
                interval = literal_parameter_value(
                    self._command.params.get("interval_ms", 1000)
                )
                return (
                    f"Stop after {self._format_duration(timeout)} · "
                    f"every {self._format_duration(interval)}"
                )
            if command_id == "win_click_image":
                return (
                    "Immediate background click"
                    if self._command.params.get("mode", "message") == "message"
                    else "Physical input timing"
                )
            if command_id == "win_drag_image":
                duration = literal_parameter_value(
                    self._command.params.get("drag_duration_ms", 300)
                )
                steps = literal_parameter_value(
                    self._command.params.get("drag_steps", 12)
                )
                return f"{self._format_duration(duration)} drag · {steps} steps"
            if command_id == "find_image":
                pause = literal_parameter_value(
                    self._command.params.get("pause_ms", 0)
                )
                minimum = literal_parameter_value(
                    self._command.params.get("min_wait_time", 0)
                )
                parts = []
                if self._positive_number(pause):
                    parts.append(f"Pause {self._format_duration(pause)}")
                if self._positive_number(minimum):
                    parts.append(f"Minimum wait {self._format_duration(minimum)}")
                return " · ".join(parts) if parts else "No added wait"
        if section.key == "update" and command_id == "update_variable":
            operation = str(self._command.params.get("operation", "set"))
            return operation.replace("_", " ").title()
        if section.key == "failure":
            if command_id == "read_screen_text":
                return f"On Error: {self._command.on_error}"
            behavior_name = (
                "condition_unmet_behavior"
                if command_id in {"if_image", "while", "win_if_image", "win_while"}
                else "not_found_behavior"
            )
            behavior = literal_parameter_value(
                self._command.params.get(behavior_name, "normal")
            )
            if behavior == "attention":
                return "Request attention"
            if behavior == "fail":
                return "Fail the flow"
            if command_id == "find_image":
                return 'Continue with "not found"'
            if command_id == "if_image":
                return "Run Else branch"
            if command_id == "while":
                return "End the loop"
            if command_id == "win_if_image":
                return "Run Else branch"
            if command_id == "win_while":
                return "End the loop"
            if command_id in {"win_click_image", "win_drag_image"}:
                return f"On Error: {self._command.on_error}"
            return f"Normal handling; on error: {self._command.on_error}"
        return section.summary

    def _windows_input_mode_summary(self) -> str:
        return {
            "message": "background",
            "input": "physical input",
            "cursor": "cursor fallback",
        }.get(str(self._command.params.get("mode", "message")), "configured")

    @staticmethod
    def _positive_number(value: Any) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value > 0
        )

    @staticmethod
    def _format_duration(value: Any) -> str:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return "configured duration"
        milliseconds = float(value)
        if milliseconds >= 1000 and milliseconds % 1000 == 0:
            return f"{milliseconds / 1000:g} s"
        return f"{milliseconds:g} ms"

    def _build_presentation_row(
        self,
        row: PresentationRow,
        definitions: dict[str, dict[str, Any]],
    ) -> QWidget | None:
        available_fields = tuple(
            item for item in row.fields if item.name in definitions
        )
        if not available_fields:
            return None
        container = QWidget()
        container.setObjectName("guidedParameterRow")
        field_visibilities = tuple(
            field_is_visible(item, self._command.params)
            for item in available_fields
        )
        row_visible = row_is_visible(row, self._command.params) and any(
            field_visibilities
        )
        container.setProperty("presentationVisible", row_visible)
        if not row_visible:
            container.hide()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        if row.label:
            label = QLabel(row.label)
            label.setFixedWidth(110)
            label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            )
            label.setStyleSheet(
                f"color: {COLORS['text_tertiary']}; font-size: 10pt; "
                "font-weight: 600; padding-top: 20px;"
            )
            layout.addWidget(label)

        for item, field_visible in zip(
            available_fields,
            field_visibilities,
            strict=True,
        ):
            if field_visible:
                definition = definitions[item.name]
                if item.label:
                    definition = {**definition, "label": item.label}
                widget = self._create_param_editor(definition)
            else:
                # Keep a lightweight field marker for presentation/state tests,
                # but do not instantiate an editor for an inactive branch.  In
                # particular, locator editors may create nested controls even
                # when their outer row is hidden.
                widget = QWidget()
            widget.setObjectName(f"commandField_{item.name}")
            widget.setProperty("presentationVisible", field_visible)
            self._apply_field_width(widget, item)
            layout.addWidget(
                widget,
                1 if item.width == "full" else 0,
                Qt.AlignmentFlag.AlignTop,
            )
            if not field_visible:
                widget.hide()
        layout.addStretch()
        return container

    @staticmethod
    def _apply_field_width(
        widget: ParamEditorWidget,
        presentation: FieldPresentation,
    ) -> None:
        maximum_widths = {
            "compact": 160,
            "short": 190,
            "medium": 250,
        }
        maximum = maximum_widths.get(presentation.width)
        if maximum is None:
            widget.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Preferred,
            )
            return
        if presentation.width == "compact":
            widget.setFixedWidth(maximum)
            return
        widget.setMaximumWidth(maximum)
        widget.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )

    def _switch_case_allowed_value_types(self) -> list[str]:
        parent_switch = self._editor._find_parent_command(
            self._editor._flow.commands,
            self._command,
        )
        if parent_switch is None or parent_switch.id != "switch_value":
            return []
        try:
            source_reference = value_reference_from_dict(
                parent_switch.params.get("source")
            )
            source_type = resolve_value_reference_type(
                source_reference,
                self._editor._flow.value_catalog,
                builtin_types=self._visible_builtin_types(),
            )
        except ValueReferenceError:
            return []
        if source_type in {ValueType.INTEGER, ValueType.NUMBER}:
            return [ValueType.INTEGER.value, ValueType.NUMBER.value]
        return [source_type.value]

    def _schedule_params_rebuild(self) -> None:
        if not self._params_rebuild_timer.isActive():
            self._params_rebuild_timer.start(0)

    def _rebuild_params(self) -> None:
        old_container = self._params_container
        layout_index = self._main_layout.indexOf(old_container)
        if layout_index < 0:
            return
        self._main_layout.removeWidget(old_container)
        old_container.setParent(None)
        old_container.deleteLater()
        self._build_params(
            self._command_types.get(self._command.id, {}),
            layout_index=layout_index,
        )
        self._params_container.setVisible(not self._collapsed)

    @staticmethod
    def _inferred_param_group(pdef: dict[str, Any]) -> str | None:
        name = str(pdef.get("name", "")).lower()
        label = str(pdef.get("label", "")).lower()
        if name in {"images", "image_name", "threshold"}:
            return "Matching"
        if name in {"color_match", "color_tolerance"}:
            return "Color Matching"
        if name in {"pause_ms", "min_wait_time", "timeout_ms", "timeout"}:
            return "Timing"
        if "wait" in label or "timeout" in label:
            return "Timing"
        return None

    @staticmethod
    def _param_order_key(index: int, pdef: dict[str, Any]) -> tuple[int, int]:
        name = str(pdef.get("name", "")).lower()
        priority = {
            "images": 0,
            "image_name": 0,
            "color_match": 1,
            "color_tolerance": 2,
            "threshold": 3,
            "click": 0,
            "clickiffound": 0,
            "click_if_found": 0,
            "click_type": 0,
            "offset_unit": 1,
            "mode": 2,
            "xmargin": 3,
            "ymargin": 4,
            "hold_duration_ms": 5,
            "pause_ms": 0,
            "timeout_ms": 1,
            "timeout": 1,
            "min_wait_time": 2,
        }
        return priority.get(name, 100), index

    @staticmethod
    def _group_param_defs(
        param_defs: list[dict[str, Any]],
    ) -> list[tuple[str, list[dict[str, Any]]]]:
        groups: list[tuple[str, list[tuple[int, dict[str, Any]]]]] = []
        index_by_title: dict[str, int] = {}
        for index, pdef in enumerate(param_defs):
            title = pdef.get("group") or CommandCard._inferred_param_group(pdef)
            if not title:
                title = "Options" if pdef.get("optional") else "Main"
            if title not in index_by_title:
                index_by_title[title] = len(groups)
                groups.append((title, []))
            groups[index_by_title[title]][1].append((index, pdef))
        ordered_groups = sorted(
            groups,
            key=lambda group: (
                0 if group[0] == "Main" else 2 if group[0] == "Options" else 1,
                index_by_title[group[0]],
            ),
        )
        return [
            (
                title,
                [
                    pdef
                    for index, pdef in sorted(
                        indexed_defs,
                        key=lambda item: CommandCard._param_order_key(item[0], item[1]),
                    )
                ],
            )
            for title, indexed_defs in ordered_groups
        ]

    def _build_param_group(self, title: str, param_defs: list[dict[str, Any]]) -> QWidget:
        group = QFrame()
        is_boxed = title not in {"Main"}
        if is_boxed:
            group.setObjectName("ParamGroupBox")
            group.setStyleSheet(
                f"#ParamGroupBox {{ background-color: {self._category_bg}; "
                f"border: 1px solid {COLORS['border']}; border-radius: 6px; }}"
            )
        group_layout = QVBoxLayout(group)
        if is_boxed:
            group_layout.setContentsMargins(8, 6, 8, 8)
            group_layout.setSpacing(6)
        else:
            group_layout.setContentsMargins(0, 0, 0, 0)
            group_layout.setSpacing(4)

        label = QLabel(title)
        label.setStyleSheet(
            f"color: {self._category_color}; font-size: 10pt; "
            "font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em;"
        )
        group_layout.addWidget(label)

        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        columns = 4
        for col in range(columns):
            grid.setColumnStretch(col, 1)

        row = 0
        col = 0
        for pdef in param_defs:
            span = self._param_grid_span(pdef)
            if col + span > columns:
                row += 1
                col = 0

            param_name = pdef["name"]
            if self._command.id == "extract_text":
                target = self._command.params.get("target", "locator")
                if target == "current_element" and param_name == "locator":
                    continue
                if target != "current_element" and param_name == "child_selector":
                    continue
            widget = self._create_param_editor(pdef)
            grid.addWidget(widget, row, col, 1, span)

            col += span
            if col >= columns:
                row += 1
                col = 0

        group_layout.addWidget(grid_host)
        return group

    def _create_param_editor(
        self,
        param_def: dict[str, Any],
    ) -> ParamEditorWidget:
        param_name = str(param_def["name"])
        value = self._command.params.get(param_name)
        if value is None:
            for alias in param_def.get("aliases", []):
                if alias in self._command.params:
                    value = self._command.params[alias]
                    break
        launcher = getattr(self._editor, "_launch_capture_target", None)
        capture_target_launcher = (
            (
                lambda name, command=self._command, launch=launcher: (
                    launch(command, name)
                )
            )
            if callable(launcher)
            else None
        )
        dynamic_launcher = getattr(
            self._editor,
            "_launch_dynamic_target_recorder",
            None,
        )
        dynamic_target_launcher = (
            (
                lambda name, command=self._command, launch=dynamic_launcher: (
                    launch(command, name)
                )
            )
            if callable(dynamic_launcher)
            else None
        )
        package_launcher = getattr(
            self._editor,
            "_launch_android_package_picker",
            None,
        )
        android_package_launcher = (
            (
                lambda name, command=self._command, launch=package_launcher: (
                    launch(command, name)
                )
            )
            if callable(package_launcher)
            else None
        )
        visual_rule_editor = getattr(
            self._editor,
            "_launch_visual_rule_editor",
            None,
        )
        visual_rule_launcher = (
            (
                lambda name, current, command=self._command, launch=visual_rule_editor: (
                    launch(command, name, current)
                )
            )
            if callable(visual_rule_editor)
            else None
        )
        return ParamEditorWidget(
            param_def,
            value,
            self._on_param_changed,
            db=self._db,
            capture_target_launcher=capture_target_launcher,
            dynamic_target_launcher=dynamic_target_launcher,
            visual_rule_launcher=visual_rule_launcher,
            android_package_launcher=android_package_launcher,
            value_catalog=getattr(self._editor._flow, "value_catalog", None),
            create_variable=getattr(
                self._editor,
                "_create_flow_variable_for_types",
                None,
            ),
            builtin_types=self._visible_builtin_types(),
            hook_choices=[
                (hook.hook_id, hook.name)
                for hook in getattr(self._editor._flow, "hooks", ())
            ],
        )

    @staticmethod
    def _param_grid_span(param_def: dict[str, Any]) -> int:
        ptype = param_def.get("type")
        name = str(param_def.get("name", ""))
        if ptype in {
            "image_list",
            "hook_list",
            "key_list",
            "string_list",
            "multiline",
            "value_reference",
            "value_template",
        }:
            return 4
        if name in {"script", "values"}:
            return 4
        if name in {"url", "selector", "text", "save_path"}:
            return 2
        return 1

    def _visible_builtin_types(self) -> dict[str, ValueType]:
        return builtin_types_for_step(
            self._editor._flow.commands,
            self._command.step_id,
            self._command_types,
            inherited=getattr(self._editor, "_base_builtin_types", {}),
        )

    # ── On Error ────────────────────────────────────────────────────────

    def _build_error_section(self) -> None:
        self._main_layout.addWidget(self._create_error_policy_widget(standalone=True))

    def _create_error_policy_widget(self, *, standalone: bool = False) -> QWidget:
        self._error_container = QWidget()
        self._error_container.setObjectName("commandErrorPolicy")
        self._error_container.setStyleSheet(
            f"background-color: {self._category_bg}; border-radius: 0;"
        )
        err_layout = QHBoxLayout(self._error_container)
        err_layout.setContentsMargins(12 if standalone else 0, 0, 12, 8)
        err_layout.setSpacing(8)

        self._error_combo = NoScrollComboBox()
        self._error_combo.setObjectName("commandOnErrorCombo")
        self._error_combo.addItems(["stop", "skip", "retry"])
        self._error_combo.setCurrentText(self._command.on_error)
        self._error_combo.currentTextChanged.connect(self._on_error_changed)
        if self._command.id == "switch_value":
            self._error_combo.setToolTip(
                "Retry reruns Switch evaluation and the selected branch from its beginning."
            )

        self._retry_spin = NoScrollSpinBox()
        self._retry_spin.setRange(0, 100)
        self._retry_spin.setValue(self._command.retry_count)
        self._retry_spin.valueChanged.connect(self._on_retry_changed)
        visible = self._command.on_error == "retry"

        if standalone:
            err_label = QLabel("On Error")
            err_label.setFixedWidth(96)
            err_label.setStyleSheet(
                f"color: {COLORS['text_secondary']}; font-size: 10pt;"
            )
            err_layout.addWidget(err_label)
            err_layout.addWidget(self._error_combo)

            retry_label = QLabel("Retries:")
            retry_label.setStyleSheet(
                f"color: {COLORS['text_secondary']}; font-size: 10pt;"
            )
            err_layout.addWidget(retry_label)
            err_layout.addWidget(self._retry_spin)
            self._retry_field = None
            self._retry_label = retry_label
            retry_label.setVisible(visible)
            self._retry_spin.setVisible(visible)
        else:
            on_error_field = QWidget()
            on_error_field.setObjectName("commandOnErrorField")
            on_error_field.setFixedWidth(190)
            on_error_layout = QVBoxLayout(on_error_field)
            on_error_layout.setContentsMargins(0, 0, 0, 0)
            on_error_layout.setSpacing(4)
            err_label = QLabel("On Error")
            err_label.setStyleSheet(
                f"color: {COLORS['text_secondary']}; font-size: 10pt;"
            )
            on_error_layout.addWidget(err_label)
            on_error_layout.addWidget(self._error_combo)
            err_layout.addWidget(on_error_field, 0, Qt.AlignmentFlag.AlignTop)

            retry_field = QWidget()
            retry_field.setObjectName("commandRetryField")
            retry_field.setFixedWidth(190)
            retry_layout = QVBoxLayout(retry_field)
            retry_layout.setContentsMargins(0, 0, 0, 0)
            retry_layout.setSpacing(4)
            retry_label = QLabel("Retries")
            retry_label.setStyleSheet(
                f"color: {COLORS['text_secondary']}; font-size: 10pt;"
            )
            retry_layout.addWidget(retry_label)
            retry_layout.addWidget(self._retry_spin)
            err_layout.addWidget(retry_field, 0, Qt.AlignmentFlag.AlignTop)
            self._retry_field = retry_field
            self._retry_label = retry_label
            retry_field.setVisible(visible)

        err_layout.addStretch()
        return self._error_container

    # ── Children ────────────────────────────────────────────────────────

    def _build_children_section(self) -> None:
        self._children_container = QWidget()
        children_layout = QVBoxLayout(self._children_container)
        children_layout.setContentsMargins(44, 2, 6, 6)
        children_layout.setSpacing(2)

        children_layout.addWidget(
            self._build_lane_header(
                "Commands" if self._is_switch_branch else "Then",
                lambda: self._editor._add_child_command(self._command, "children"),
            )
        )

        body = BranchDropBody(self, "children")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(10, 2, 0, 2)
        body_layout.setSpacing(2)

        for i, child_cmd in enumerate(self._command.children):
            card = CommandCard(
                child_cmd,
                self._command_types,
                self._editor,
                depth=self._depth + 1,
                index=i,
                db=self._db,
            )
            body_layout.addWidget(card)

        if not self._command.children:
            empty = QLabel("Drop or add commands here")
            empty.setStyleSheet(
                f"color: {COLORS['text_tertiary']}; font-size: 10pt; padding: 4px 0;"
            )
            body_layout.addWidget(empty)

        children_layout.addWidget(body)

        self._main_layout.addWidget(self._children_container)

    def _build_switch_branches_section(self) -> None:
        self._children_container = QWidget()
        branches_layout = QVBoxLayout(self._children_container)
        branches_layout.setContentsMargins(44, 2, 6, 6)
        branches_layout.setSpacing(4)

        lane = QWidget()
        lane_layout = QHBoxLayout(lane)
        lane_layout.setContentsMargins(0, 2, 0, 2)
        lane_layout.setSpacing(8)
        label = QLabel("CASES")
        label.setStyleSheet(
            f"color: {self._category_color}; font-size: 10pt; font-weight: 800;"
        )
        lane_layout.addWidget(label)
        add_case = QPushButton("Add Case")
        add_case.setObjectName("addSwitchCaseButton")
        add_case.setToolTip("Add a Case before Default")
        add_case.clicked.connect(
            lambda: self._editor._add_switch_case(self._command)
        )
        self._lane_add_buttons.append(add_case)
        lane_layout.addWidget(add_case)
        lane_layout.addStretch()
        branches_layout.addWidget(lane)

        for index, branch in enumerate(self._command.children):
            branches_layout.addWidget(
                CommandCard(
                    branch,
                    self._command_types,
                    self._editor,
                    depth=self._depth + 1,
                    index=index,
                    db=self._db,
                )
            )
        self._main_layout.addWidget(self._children_container)

    # ── Else section ────────────────────────────────────────────────────

    def _build_else_section(self) -> None:
        self._else_container = QWidget()
        else_layout = QVBoxLayout(self._else_container)
        else_layout.setContentsMargins(44, 0, 6, 6)
        else_layout.setSpacing(2)

        else_layout.addWidget(
            self._build_lane_header(
                "Else",
                lambda: self._editor._add_child_command(self._command, "else_children"),
                COLORS["orange"],
            )
        )

        else_body = BranchDropBody(self, "else_children")
        else_body_layout = QVBoxLayout(else_body)
        else_body_layout.setContentsMargins(10, 2, 0, 2)
        else_body_layout.setSpacing(2)

        for i, child_cmd in enumerate(self._command.else_children):
            card = CommandCard(
                child_cmd,
                self._command_types,
                self._editor,
                depth=self._depth + 1,
                index=i,
                db=self._db,
            )
            else_body_layout.addWidget(card)

        if not self._command.else_children:
            empty = QLabel("Drop or add commands here")
            empty.setStyleSheet(
                f"color: {COLORS['text_tertiary']}; font-size: 10pt; padding: 4px 0;"
            )
            else_body_layout.addWidget(empty)

        else_layout.addWidget(else_body)
        self._main_layout.addWidget(self._else_container)

    def _build_lane_header(
        self,
        title: str,
        add_callback: Any,
        accent: str | None = None,
    ) -> QWidget:
        if accent is None:
            accent = self._category_color
        lane = QWidget()
        lane_layout = QHBoxLayout(lane)
        lane_layout.setContentsMargins(0, 2, 0, 0)
        lane_layout.setSpacing(8)

        label = QLabel(title.upper())
        label.setStyleSheet(
            f"color: {accent}; font-size: 10pt; font-weight: 800; "
            "letter-spacing: 0.04em; background: transparent;"
        )
        lane_layout.addWidget(label)

        add_btn = QPushButton("+ add")
        add_btn.setFixedHeight(28)
        add_btn.setStyleSheet(
            f"QPushButton {{ color: {COLORS['text_secondary']}; "
            f"border: 1px dashed {COLORS['border']}; border-radius: 4px; "
            f"padding: 1px 8px; background: transparent; font-size: 10pt; }}"
            f"QPushButton:hover {{ color: {COLORS['primary']}; border-color: {COLORS['primary']}; }}"
        )
        add_btn.clicked.connect(add_callback)
        self._lane_add_buttons.append(add_btn)
        lane_layout.addWidget(add_btn)
        lane_layout.addStretch()
        return lane

    # ── Collapse / Expand ───────────────────────────────────────────────

    def _header_clicked(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if hasattr(self._editor, "_select_command_step"):
                self._editor._select_command_step(self._command.step_id, scroll=False)
            self._toggle_collapsed()

    def _toggle_collapsed(self) -> None:
        self._collapsed = not self._collapsed
        self._command.collapsed = self._collapsed
        self._apply_collapsed_visibility()

    def _update_expand_button_accessibility(self) -> None:
        action = "Expand" if self._collapsed else "Collapse"
        self._expand_btn.setToolTip(f"{action} command")
        self._expand_btn.setAccessibleName(
            f"{action} command: {self._label_text}"
        )
        self._expand_btn.setAccessibleDescription(
            "Shows this command's settings."
            if self._collapsed
            else "Hides this command's settings."
        )

    def _apply_collapsed_visibility(self) -> None:
        """Show or hide sections based on collapsed state."""
        collapsed = self._collapsed
        self._params_container.setVisible(not collapsed)
        hooks_container = getattr(self, "_hooks_container", None)
        if hooks_container is not None:
            hooks_container.setVisible(not collapsed)
        if self._is_switch_branch:
            self._children_container.setVisible(not collapsed)
            if hasattr(self, "_comment_edit"):
                self._comment_edit.setVisible(not collapsed)
            if hasattr(self, "_expand_btn"):
                self._expand_btn.setText("\u25be" if collapsed else "\u25b4")
                self._update_expand_button_accessibility()
            return
        self._error_container.setVisible(not collapsed)
        if self._can_have_children and hasattr(self, "_children_container"):
            self._children_container.setVisible(
                not collapsed if self._collapse_children else True
            )
        if self._can_have_else and hasattr(self, "_else_container"):
            self._else_container.setVisible(True)
        if hasattr(self, "_expand_btn"):
            self._expand_btn.setText("\u25be" if collapsed else "\u25b4")
            self._update_expand_button_accessibility()

        if collapsed:
            self._comment_edit.setVisible(False)
            self._update_collapsed_info()
        else:
            self._comment_edit.setVisible(True)
            self._collapsed_info.setVisible(False)

    # ── Callbacks ───────────────────────────────────────────────────────

    def _on_param_changed(self, name: str, value: Any) -> None:
        if self._command.id == "update_variable":
            self._command.params = normalize_variable_update_editor_change(
                self._command.params,
                name,
                value,
                self._editor._flow.value_catalog,
            )
            if name in {"target_variable_id", "operation"}:
                self._schedule_params_rebuild()
        elif self._command.id in {"if_value", "while_value", "switch_case"}:
            self._command.params = normalize_value_condition_editor_change(
                self._command.params,
                name,
                value,
            )
            if name == "operator":
                self._schedule_params_rebuild()
        elif self._command.id == "extract_text":
            self._command.params[name] = value
            if name == "target":
                if value == "current_element":
                    self._command.params.pop("locator", None)
                    self._command.params.pop("selector", None)
                else:
                    self._command.params.pop("child_selector", None)
                self._schedule_params_rebuild()
        elif self._command.id == "switch_value" and name == "source":
            self._command.params[name] = value
            QTimer.singleShot(0, self._editor._rebuild_cards)
        else:
            self._command.params[name] = value
            if self._command.id in {
                "click_image",
                "find_image",
                "if_image",
                "while",
                "win_click_image",
                "win_drag_image",
                "win_if_image",
                "win_while",
                "read_screen_text",
                "visual_hook_condition",
            } and name in {
                "color_match",
                "condition",
                "clickIfFound",
                "click_if_found",
                "click",
                "button",
                "mode",
                "search_region_mode",
                "search_anchor_color_match",
                "not_found_behavior",
                "condition_unmet_behavior",
                "timeout",
                "interval_ms",
                "drag_duration_ms",
                "drag_steps",
                "region_mode",
                "result_type",
                "pause_ms",
                "min_wait_time",
            }:
                self._schedule_params_rebuild()
        presentation = command_presentation(self._command.id)
        if (
            presentation is not None
            and name in presentation_visibility_parameters(presentation)
        ):
            self._schedule_params_rebuild()
        self._update_summary()
        self._update_switch_case_header_summary()
        if hasattr(self._editor, "_on_command_metadata_changed"):
            self._editor._on_command_metadata_changed(self._command)

    def _on_run_flow_binding_changed(self, binding_params: dict[str, object]) -> None:
        global_params = {
            name: self._command.params[name]
            for name in ("pause_ms", "min_wait_time")
            if name in self._command.params
        }
        self._command.params = {**binding_params, **global_params}
        self._update_summary()
        if hasattr(self._editor, "_on_command_metadata_changed"):
            self._editor._on_command_metadata_changed(self._command)

    def _on_comment_changed(self, text: str) -> None:
        self._command.comment = text
        self._update_summary()
        self._update_switch_case_header_summary()
        if hasattr(self._editor, "_on_command_metadata_changed"):
            self._editor._on_command_metadata_changed(self._command)

    def _on_error_changed(self, text: str) -> None:
        self._command.on_error = text
        is_retry = text == "retry"
        if self._retry_field is not None:
            self._retry_field.setVisible(is_retry)
        else:
            self._retry_label.setVisible(is_retry)
        self._retry_spin.setVisible(is_retry)
        if self._command.id in {
            "click_image",
            "find_image",
            "if_image",
            "while",
            "win_click_image",
            "win_drag_image",
            "win_if_image",
            "win_while",
            "read_screen_text",
        }:
            self._schedule_params_rebuild()
        if hasattr(self._editor, "_on_command_metadata_changed"):
            self._editor._on_command_metadata_changed(self._command)

    def _on_retry_changed(self, value: int) -> None:
        self._command.retry_count = value
        if hasattr(self._editor, "_on_command_metadata_changed"):
            self._editor._on_command_metadata_changed(self._command)

    def _on_breakpoint_toggle(self, checked: bool) -> None:
        if self._is_run_step_busy():
            return
        self._command.breakpoint = checked
        if hasattr(self._editor, "_on_command_metadata_changed"):
            self._editor._on_command_metadata_changed(self._command)

    def _on_enabled_toggle(self, checked: bool) -> None:
        if self._is_run_step_busy():
            if hasattr(self, "_enabled_btn"):
                self._enabled_btn.blockSignals(True)
                self._enabled_btn.setChecked(self._command.enabled)
                self._enabled_btn.blockSignals(False)
            return
        self._command.enabled = checked
        if hasattr(self, "_enabled_btn"):
            self._enabled_btn.blockSignals(True)
            self._enabled_btn.setChecked(checked)
            self._enabled_btn.setText("On" if checked else "Off")
            self._enabled_btn.blockSignals(False)
        if hasattr(self, "_action_label"):
            self._action_label.setStyleSheet(self._tag_style())
        if hasattr(self, "_category_label"):
            self._category_label.setStyleSheet(self._category_label_style())
        if hasattr(self, "_summary_label"):
            self._summary_label.setStyleSheet(self._summary_label_style())
        if hasattr(self, "_run_btn"):
            self._run_btn.setEnabled(checked)
            self._run_btn.setToolTip("Run this step" if checked else "Command is disabled")
        self._apply_default_style()
        self._update_summary()
        if hasattr(self._editor, "_on_command_metadata_changed"):
            self._editor._on_command_metadata_changed(self._command)

    def _on_insert_above(self) -> None:
        if self._is_run_step_busy():
            return
        self._editor._insert_command_above(self._command)

    def _on_duplicate(self) -> None:
        if self._is_run_step_busy():
            return
        self._editor._duplicate_command(self._command)

    def _on_move_to(self) -> None:
        if self._is_run_step_busy():
            return
        if hasattr(self._editor, "_show_move_to_dialog"):
            self._editor._show_move_to_dialog(self._command)

    def _on_delete(self) -> None:
        if self._is_run_step_busy():
            return
        cmd_def = self._command_types.get(self._command.id, {})
        label = cmd_def.get("label", self._command.id)
        reply = QMessageBox.question(
            self,
            "Delete Step",
            f"Delete '{label}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._editor._remove_command(self._command)

    def _on_run_step(self) -> None:
        controller = getattr(self._editor, "_run_step_controller", None)
        if (
            controller is not None
            and controller.is_busy
            and controller.active_step_id == self._command.step_id
        ):
            self._editor.run_single_command(self._command)
            return
        if not self._command.enabled:
            QMessageBox.information(
                self,
                "Command Disabled",
                "Enable this command before running it directly.",
            )
            return
        if hasattr(self._editor, "run_single_command"):
            self._editor.run_single_command(self._command)

    def _on_run_step_state_changed(
        self,
        active_step_id: str,
        state: str,
    ) -> None:
        is_active = active_step_id == self._command.step_id
        if state == "idle":
            self._set_run_step_controls_locked(False)
            self._run_btn.setText("\u25b6")
            self._run_btn.setStyleSheet(self._run_btn_style)
            self._run_btn.setEnabled(self._command.enabled)
            self._run_btn.setToolTip(
                "Run this step" if self._command.enabled else "Command is disabled"
            )
            self._run_btn.setAccessibleName(f"Run step: {self._label_text}")
            return
        self._set_run_step_controls_locked(True)
        if not is_active:
            self._run_btn.setText("\u25b6")
            self._run_btn.setStyleSheet(self._run_btn_style)
            self._run_btn.setEnabled(False)
            self._run_btn.setToolTip("Another step is currently running")
            self._run_btn.setAccessibleName(
                f"Run step unavailable: {self._label_text}"
            )
            return
        self._run_btn.setStyleSheet(self._cancel_run_btn_style)
        if state == "cleanup_pending":
            self._run_btn.setText("\u21bb")
            self._run_btn.setEnabled(True)
            self._run_btn.setToolTip("Retry device cleanup")
            self._run_btn.setAccessibleName(
                f"Retry cleanup for running step: {self._label_text}"
            )
            return
        if state == "cancel_requested":
            self._run_btn.setText("\u2026")
            self._run_btn.setEnabled(False)
            self._run_btn.setToolTip("Stopping this step")
            self._run_btn.setAccessibleName(
                f"Stopping running step: {self._label_text}"
            )
            return
        self._run_btn.setText("\u25a0")
        self._run_btn.setEnabled(True)
        self._run_btn.setToolTip("Stop this running step")
        self._run_btn.setAccessibleName(
            f"Stop running step: {self._label_text}"
        )

    def _is_run_step_busy(self) -> bool:
        controller = getattr(self._editor, "_run_step_controller", None)
        return bool(controller is not None and controller.is_busy)

    def _set_run_step_controls_locked(self, locked: bool) -> None:
        if locked == self._run_step_controls_locked:
            return
        controls: list[QWidget | QAction] = [
            self._enabled_btn,
            self._insert_btn,
            self._more_btn,
            self._drag_handle,
            self._comment_edit,
            self._params_container,
            self._error_container,
            *self._lane_add_buttons,
            *self._actions_menu.actions(),
        ]
        preview_button = getattr(self, "_preview_btn", None)
        if preview_button is not None:
            controls.append(preview_button)
        if locked:
            self._run_step_control_states = [
                (control, control.isEnabled()) for control in controls
            ]
            for control, _was_enabled in self._run_step_control_states:
                control.setEnabled(False)
            self.setAcceptDrops(False)
        else:
            for control, was_enabled in self._run_step_control_states:
                control.setEnabled(was_enabled)
            self._run_step_control_states = []
            self.setAcceptDrops(True)
        self._run_step_controls_locked = locked

    # ── Drop zone detection ─────────────────────────────────────────────

    def _detect_drop_zone(self, pos: QPoint) -> str | None:
        """Determine which drop zone the cursor is in based on its Y position."""
        h = self.height()
        y = pos.y()

        if self._command.id == "switch_value":
            return DROP_ZONE_ABOVE if y < h * 0.50 else DROP_ZONE_BELOW
        if self._can_have_children:
            # Top 20% = above, bottom 20% = below, middle 60% = into children
            if y < h * 0.20:
                return DROP_ZONE_ABOVE
            elif y > h * 0.80:
                return DROP_ZONE_BELOW
            else:
                return DROP_ZONE_INTO
        else:
            # No children support: top 50% = above, bottom 50% = below
            if y < h * 0.50:
                return DROP_ZONE_ABOVE
            else:
                return DROP_ZONE_BELOW

    # ── Drag and Drop ───────────────────────────────────────────────────

    def _drag_handle_pressed(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.position().toPoint()
            event.accept()
            return
        event.ignore()

    def _drag_handle_moved(self, event: Any) -> None:
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            event.ignore()
            return
        if self._drag_start_pos is None:
            self._drag_start_pos = event.position().toPoint()
            event.accept()
            return
        distance = (event.position().toPoint() - self._drag_start_pos).manhattanLength()
        if distance < QApplication.startDragDistance():
            event.accept()
            return
        self._start_card_drag()
        self._drag_start_pos = None
        event.accept()

    def _start_card_drag(self) -> None:
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(MIME_COMMAND_STEP_ID, self._command.step_id.encode("utf-8"))
        drag.setMimeData(mime)
        if hasattr(self._editor, "_start_drag_auto_scroll"):
            self._editor._start_drag_auto_scroll()
        try:
            drag.exec(Qt.DropAction.MoveAction)
        finally:
            if hasattr(self._editor, "_stop_drag_auto_scroll"):
                self._editor._stop_drag_auto_scroll()

    def mouseMoveEvent(self, event: Any) -> None:
        super().mouseMoveEvent(event)

    def _can_accept_drop(self, moved: FlowCommand | None) -> bool:
        """Return whether *moved* has a valid structural target on this card."""
        if moved is None or moved.step_id == self._command.step_id:
            return False
        if moved.id == "switch_default":
            return False
        if moved.id != "switch_case":
            return True
        if self._command.id not in {"switch_case", "switch_default"}:
            return False
        source_switch = self._editor._find_parent_command(
            self._editor._flow.commands,
            moved,
        )
        target_switch = self._editor._find_parent_command(
            self._editor._flow.commands,
            self._command,
        )
        return (
            source_switch is not None
            and source_switch is target_switch
            and source_switch.id == "switch_value"
        )

    def _dragged_command(self, event: Any) -> FlowCommand | None:
        if not event.mimeData().hasFormat(MIME_COMMAND_STEP_ID):
            return None
        step_id = bytes(event.mimeData().data(MIME_COMMAND_STEP_ID)).decode("utf-8")
        return self._editor._find_by_step_id(
            self._editor._flow.commands,
            step_id,
        )

    def dragEnterEvent(self, event: Any) -> None:
        moved = self._dragged_command(event)
        if self._can_accept_drop(moved):
            zone = self._detect_drop_zone(event.position().toPoint())
            self._set_drop_zone(zone)
            event.acceptProposedAction()
            return
        self._set_drop_zone(None)
        event.ignore()

    def dragMoveEvent(self, event: Any) -> None:
        moved = self._dragged_command(event)
        if self._can_accept_drop(moved):
            if hasattr(self._editor, "_auto_scroll_during_drag"):
                self._editor._auto_scroll_during_drag()
            zone = self._detect_drop_zone(event.position().toPoint())
            self._set_drop_zone(zone)
            event.acceptProposedAction()
            return
        self._set_drop_zone(None)
        event.ignore()

    def dragLeaveEvent(self, event: Any) -> None:
        self._set_drop_zone(None)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: Any) -> None:
        if event.mimeData().hasFormat(MIME_COMMAND_STEP_ID):
            step_id = bytes(event.mimeData().data(MIME_COMMAND_STEP_ID)).decode("utf-8")
            moved = self._dragged_command(event)
            if self._can_accept_drop(moved):
                zone = self._detect_drop_zone(event.position().toPoint())
                self._set_drop_zone(None)

                if self._is_switch_branch and moved is not None:
                    if moved.id == "switch_case":
                        position = (
                            "above"
                            if self._command.id == "switch_default"
                            or event.position().toPoint().y() < self.height() / 2
                            else "below"
                        )
                        self._editor._move_command_relative(
                            step_id,
                            self._command,
                            position,
                        )
                    else:
                        self._editor._move_command_to_parent(step_id, self._command)
                    event.acceptProposedAction()
                    return

                if zone == DROP_ZONE_ABOVE:
                    self._editor._move_command_relative(step_id, self._command, "above")
                elif zone == DROP_ZONE_BELOW:
                    self._editor._move_command_relative(step_id, self._command, "below")
                elif zone == DROP_ZONE_INTO:
                    self._editor._move_command_to_parent(step_id, self._command)
                event.acceptProposedAction()
                return
        self._set_drop_zone(None)
        event.ignore()

    # ── Visual drop feedback ────────────────────────────────────────────

    def _set_drop_zone(self, zone: str | None) -> None:
        if zone == self._drop_zone:
            return
        self._drop_zone = zone
        self.update()  # trigger repaint

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        if self._drop_zone is None:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self._drop_zone == DROP_ZONE_INTO:
            # Highlight entire card with a blue border
            pen = QPen(QColor(COLORS["accent"]), 3)
            painter.setPen(pen)
            painter.setBrush(QColor(88, 166, 255, 30))  # translucent blue fill
            painter.drawRoundedRect(2, 2, self.width() - 4, self.height() - 4, 6, 6)
        else:
            # Draw a thick blue line at top or bottom
            pen = QPen(QColor(COLORS["accent"]), 3)
            painter.setPen(pen)
            if self._drop_zone == DROP_ZONE_ABOVE:
                y = 1
            else:
                y = self.height() - 2
            painter.drawLine(8, y, self.width() - 8, y)

            # Draw a small circle at the left end of the line
            painter.setBrush(QColor(COLORS["accent"]))
            painter.drawEllipse(4, y - 4, 8, 8)

        painter.end()
