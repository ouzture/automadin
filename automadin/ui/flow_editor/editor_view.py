"""Main flow editor view -- visual flow builder with drag-drop command cards."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from typing import Any

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QCursor, QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QHeaderView,
    QTreeWidget,
    QTreeWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from automadin.engine.session import DeviceSessionService
from automadin.execution.flow_values import FlowRuntimeValueError
from automadin.execution.debug_context import build_debug_context
from automadin.execution.handlers.registry import get_default_params, load_command_types
from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    AutomationFlow,
    FlowCommand,
)
from automadin.models.flow_value import FlowValueCatalog, FlowValueDeclarationError
from automadin.models.hook import (
    AutomationHook,
    HookAutomaticScope,
    HookBinding,
    HookPhase,
    HookType,
    VisualImageHookCondition,
)
from automadin.models.typed_value import ValueType
from automadin.models.resource_path import (
    find_resource_collision,
    normalize_resource_path,
    resource_display_label,
    resource_group_path,
    safe_export_stem,
)
from automadin.services.database import (
    DatabaseService,
    ImageAlreadyExistsError,
    ImageRevisionConflictError,
    StoredImage,
)
from automadin.services.flow_preflight import (
    PreflightMode,
    preflight_flow,
    target_kind_for_device,
)
from automadin.ui.flow_editor.command_card import MIME_COMMAND_STEP_ID, CommandCard
from automadin.ui.flow_editor.command_presentation import (
    validate_command_presentations,
)
from automadin.ui.flow_editor.flow_parameters_dialog import FlowParametersDialog
from automadin.ui.flow_editor.flow_values_dialog import (
    FlowValueEditDialog,
    FlowValuesDialog,
)
from automadin.ui.flow_editor.hook_dialog import HookEditDialog, HooksManagerDialog
from automadin.ui.flow_editor.resolution_scaling_dialog import (
    ResolutionScalingDialog,
)
from automadin.ui.flow_editor.palette import CommandPaletteDialog
from automadin.ui.flow_editor.run_step_controller import (
    RunStepBusyError,
    RunStepOperationController,
    RunStepStartError,
)
from automadin.ui.preflight_dialog import (
    confirm_document_preflight,
    confirm_execution_preflight,
    confirm_import_preflight,
)
from automadin.ui.resource_path_dialog import ResourcePathDialog
from automadin.ui.resource_tree import ResourceTreeWidget
from automadin.ui.theme import CATEGORY_COLORS, COLORS
from automadin.ui.capture_controller import CaptureOperationController


def _load_command_types() -> dict[str, dict[str, Any]]:
    """Load validated command metadata."""
    command_types = load_command_types()
    validate_command_presentations(command_types)
    return command_types


class _DroppableCardsWidget(QWidget):
    """The inner widget of the scroll area that accepts drops at the end of the list."""

    def __init__(self, editor: FlowEditorView, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._editor = editor
        self.setAcceptDrops(True)
        self._show_drop = False

    def dragEnterEvent(self, event: Any) -> None:
        if event.mimeData().hasFormat(MIME_COMMAND_STEP_ID):
            self._show_drop = True
            self.update()
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: Any) -> None:
        if event.mimeData().hasFormat(MIME_COMMAND_STEP_ID):
            self._editor._auto_scroll_during_drag()
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: Any) -> None:
        self._show_drop = False
        self.update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: Any) -> None:
        self._show_drop = False
        self.update()
        if event.mimeData().hasFormat(MIME_COMMAND_STEP_ID):
            step_id = bytes(event.mimeData().data(MIME_COMMAND_STEP_ID)).decode("utf-8")
            self._editor._move_command_to_root_end(step_id)
            event.acceptProposedAction()
        else:
            event.ignore()

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        if self._show_drop and self._editor._flow.commands:
            # Draw a blue line at the bottom to show "drop here to add to end"
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            pen = QPen(QColor(COLORS["accent"]), 3)
            painter.setPen(pen)
            # Find the bottom of the last card
            layout = self.layout()
            if layout and layout.count() > 1:
                # last widget before the stretch
                last_item = layout.itemAt(layout.count() - 2)
                if last_item and last_item.widget():
                    w = last_item.widget()
                    y = w.geometry().bottom() + 4
                    painter.drawLine(24, y, self.width() - 24, y)
                    painter.setBrush(QColor(COLORS["accent"]))
                    painter.drawEllipse(20, y - 4, 8, 8)
            painter.end()


class _FlowOutlineTree(QTreeWidget):
    """Read-mostly outline tree with optional guarded command reordering."""

    def __init__(self, editor: FlowEditorView, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._editor = editor
        self._outline_drag_enabled = False
        self.set_outline_drag_enabled(False)

    def set_outline_drag_enabled(self, enabled: bool) -> None:
        self._outline_drag_enabled = enabled
        self.setDragEnabled(enabled)
        self.setAcceptDrops(enabled)
        self.setDropIndicatorShown(enabled)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragDropMode(
            QAbstractItemView.DragDropMode.InternalMove
            if enabled
            else QAbstractItemView.DragDropMode.NoDragDrop
        )

    def dropEvent(self, event: Any) -> None:
        if not self._outline_drag_enabled:
            event.ignore()
            return

        source_item = self.currentItem()
        target_item = self.itemAt(event.position().toPoint())
        if source_item is None or target_item is None:
            event.ignore()
            return

        source_step_id = source_item.data(0, Qt.ItemDataRole.UserRole)
        target_step_id = target_item.data(0, Qt.ItemDataRole.UserRole)
        if not source_step_id or not target_step_id or source_step_id == target_step_id:
            event.ignore()
            return

        ref_command = self._editor._find_by_step_id(
            self._editor._flow.commands,
            str(target_step_id),
        )
        if ref_command is None:
            event.ignore()
            return

        drop_position = self.dropIndicatorPosition()
        position = (
            "below"
            if drop_position == QAbstractItemView.DropIndicatorPosition.BelowItem
            else "above"
        )
        self._editor._move_command_relative(str(source_step_id), ref_command, position)
        event.acceptProposedAction()


class FlowEditorView(QWidget):
    """Visual flow builder with toolbar and scrollable card list."""

    resource_renamed = Signal(str, str, str)  # kind, old_name, new_name
    resource_deleted = Signal(str, str)  # kind, name
    resource_saved = Signal(str, str)  # kind, name
    flow_deleted = Signal(str)
    run_step_state_changed = Signal(str, str)  # active step id, state

    def __init__(
        self,
        db: DatabaseService,
        session_service: DeviceSessionService,
        device_selector=None,
        parent: QWidget | None = None,
        *,
        capture_controller: CaptureOperationController | None = None,
        run_step_controller: RunStepOperationController | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._session_service = session_service
        self._device_selector = device_selector  # shared from MainWindow
        self._capture_controller = capture_controller
        self._flow = AutomationFlow(
            name="Untitled",
            value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        )
        self._flow_is_saved: bool = False
        self._allow_unsaved_overwrite = False
        self._clean_snapshot = self._flow_snapshot()
        self._command_types = _load_command_types()
        self._executor = None  # lazy-built on first run
        self._run_step_controller = run_step_controller or RunStepOperationController(
            self
        )
        self._run_step_controller.state_changed.connect(
            self._on_run_step_state_changed
        )
        self._run_step_controller.succeeded.connect(
            self._on_run_step_succeeded
        )
        self._run_step_controller.failed.connect(self._on_run_step_failed)
        self._run_step_controller.cancelled.connect(
            self._on_run_step_cancelled
        )
        self._run_step_controller.probe_completed.connect(
            self._on_hook_probe_completed
        )
        self._hook_probe_operations: dict[str, tuple[str, str]] = {}
        self._card_by_step_id: dict[str, CommandCard] = {}
        self._command_section_states: dict[tuple[str, str], bool] = {}
        self._outline_item_by_step_id: dict[str, QTreeWidgetItem] = {}
        self._selected_step_id: str | None = None
        self._run_step_flow_controls_locked = False
        self._run_step_flow_control_states: list[
            tuple[QWidget | QAction, bool]
        ] = []
        self._run_step_flow_mutation_controls: list[QWidget | QAction] = []
        self._web_inspector_dialog: QDialog | None = None
        self._drag_scroll_timer = QTimer(self)
        self._drag_scroll_timer.setInterval(45)
        self._drag_scroll_timer.timeout.connect(self._auto_scroll_during_drag)
        self._document_state_timer = QTimer(self)
        self._document_state_timer.setInterval(250)
        self._document_state_timer.timeout.connect(self._refresh_document_state)
        self._document_state_timer.start()

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self._build_toolbar(main_layout)
        self._build_scroll_area(main_layout)
        self._rebuild_cards()
        self._update_saved_flow_actions()

    def _flow_snapshot(self) -> str:
        """Return a stable representation used for dirty-state detection."""
        return json.dumps(
            self._flow.to_dict(), sort_keys=True, separators=(",", ":")
        )

    @property
    def has_unsaved_changes(self) -> bool:
        return self._flow_snapshot() != self._clean_snapshot

    def _mark_clean(self) -> None:
        self._clean_snapshot = self._flow_snapshot()
        self._refresh_document_state()

    def _apply_renamed_flow_name(self, new_name: str) -> None:
        """Rename the document identity without hiding unrelated unsaved edits."""
        self.on_resource_renamed("flow", self._flow.name, new_name)

    @staticmethod
    def _replace_live_references(
        commands: list[FlowCommand],
        old_name: str,
        new_name: str,
        scalar_keys: tuple[str, ...],
        list_keys: tuple[str, ...] = (),
    ) -> bool:
        changed = False
        for command in commands:
            for key in scalar_keys:
                if command.params.get(key) == old_name:
                    command.params[key] = new_name
                    changed = True
            for key in list_keys:
                values = command.params.get(key)
                if isinstance(values, list) and old_name in values:
                    command.params[key] = [
                        new_name if value == old_name else value for value in values
                    ]
                    changed = True
            changed |= FlowEditorView._replace_live_references(
                command.children, old_name, new_name, scalar_keys, list_keys
            )
            changed |= FlowEditorView._replace_live_references(
                command.else_children, old_name, new_name, scalar_keys, list_keys
            )
        return changed

    @staticmethod
    def _replace_live_hook_references(
        flow: AutomationFlow,
        old_name: str,
        new_name: str,
        scalar_keys: tuple[str, ...],
        list_keys: tuple[str, ...],
    ) -> bool:
        changed = False
        for hook in flow.hooks:
            changed |= FlowEditorView._replace_live_references(
                hook.actions, old_name, new_name, scalar_keys, list_keys
            )
            condition = hook.condition
            if "images" in list_keys and isinstance(
                condition, VisualImageHookCondition
            ):
                values = list(condition.images)
                if old_name in values:
                    hook.condition = replace(
                        condition,
                        images=tuple(
                            new_name if value == old_name else value
                            for value in values
                        ),
                    )
                    condition = hook.condition
                    changed = True
            settings = getattr(condition, "search_settings", None)
            if isinstance(settings, dict):
                for key in list_keys:
                    values = settings.get(key)
                    if isinstance(values, list) and old_name in values:
                        settings[key] = [
                            new_name if value == old_name else value
                            for value in values
                        ]
                        changed = True
        return changed

    def _refresh_document_state(self) -> None:
        name = self._flow.name or "Untitled"
        display = resource_display_label(name) or "Untitled"
        self._flow_name_label.setText(
            f"{display}{' *' if self.has_unsaved_changes else ''}"
        )
        self._flow_name_label.setToolTip(name)
        if hasattr(self, "_flow_parameters_btn"):
            is_v1 = (
                self._flow.value_model_version
                == CURRENT_FLOW_VALUE_MODEL_VERSION
            )
            self._flow_parameters_btn.setText(
                "Inputs & Variables" if is_v1 else "Parameters"
            )
            self._flow_parameters_btn.setToolTip(
                "Declare typed Inputs and Variables for this flow"
                if is_v1
                else "Declare legacy typed parameters for this flow"
            )
        if hasattr(self, "_resolution_scaling_btn"):
            scaling = self._flow.resolution_scaling
            if scaling.mode.value == "adaptive":
                label = (
                    f"Scaling: {scaling.reference_width}×"
                    f"{scaling.reference_height}"
                )
            else:
                label = "Scaling: Off"
            self._resolution_scaling_btn.setText(label)
        if hasattr(self, "_hooks_btn"):
            self._hooks_btn.setText(f"Hooks ({len(self._flow.hooks)})")

    def confirm_discard_changes(self) -> bool:
        """Ask how to handle editor changes before replacing or closing them."""
        if not self.has_unsaved_changes:
            return True
        reply = QMessageBox.warning(
            self,
            "Unsaved Flow Changes",
            f"Save changes to '{self._flow.name or 'Untitled'}' before continuing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if reply == QMessageBox.StandardButton.Cancel:
            return False
        if reply == QMessageBox.StandardButton.Save:
            return self._save_flow()
        return True

    # ── Toolbar ─────────────────────────────────────────────────────────

    def _build_toolbar(self, parent_layout: QVBoxLayout) -> None:
        toolbar = QWidget()
        toolbar.setObjectName("flowToolbar")
        toolbar.setStyleSheet(
            f"#flowToolbar {{ background-color: {COLORS['bg_surface']}; "
            f"border-bottom: 1px solid {COLORS['border']}; }}"
        )
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(16, 10, 16, 10)
        tb_layout.setSpacing(8)

        add_btn = QPushButton("Add command")
        add_btn.setProperty("cssClass", "primary")
        add_btn.clicked.connect(self._add_command)
        self._add_command_btn = add_btn
        tb_layout.addWidget(add_btn)

        inspect_btn = QPushButton("Web Inspector")
        inspect_btn.setToolTip(
            "Pick a browser element and use its locator in a browser step"
        )
        inspect_btn.clicked.connect(self._open_web_inspector)
        self._web_inspector_btn = inspect_btn
        tb_layout.addWidget(inspect_btn)

        parameters_btn = QPushButton("Parameters")
        parameters_btn.setToolTip("Declare typed inputs for this flow")
        parameters_btn.clicked.connect(self._edit_flow_parameters)
        self._flow_parameters_btn = parameters_btn
        tb_layout.addWidget(parameters_btn)

        scaling_btn = QPushButton("Scaling: Off")
        scaling_btn.setToolTip(
            "Configure image and pixel-coordinate scaling for this flow"
        )
        scaling_btn.clicked.connect(self._edit_resolution_scaling)
        self._resolution_scaling_btn = scaling_btn
        tb_layout.addWidget(scaling_btn)

        hooks_btn = QPushButton("Hooks (0)")
        hooks_btn.setToolTip("Create and manage typed interruption hooks for this flow")
        hooks_btn.clicked.connect(self._edit_hooks)
        self._hooks_btn = hooks_btn
        tb_layout.addWidget(hooks_btn)

        self._flow_name_label = QLabel("Untitled")
        self._flow_name_label.setStyleSheet(
            f"color: {COLORS['text_primary']}; font-size: 12pt; font-weight: 650; "
            "padding-left: 8px;"
        )
        tb_layout.addWidget(self._flow_name_label)

        self._run_step_status_label = QLabel("")
        self._run_step_status_label.setAccessibleName("Run Step status")
        self._run_step_status_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10.5pt; "
            f"padding: 4px 8px; border-left: 1px solid {COLORS['border']};"
        )
        self._run_step_status_label.hide()
        tb_layout.addWidget(self._run_step_status_label)

        tb_layout.addStretch()

        new_btn = QPushButton("New")
        new_btn.setToolTip("Start a blank unsaved flow with no loaded flow name")
        new_btn.clicked.connect(self._new_flow)
        self._new_flow_btn = new_btn
        tb_layout.addWidget(new_btn)

        load_btn = QPushButton("Open")
        load_btn.clicked.connect(self._load_flow)
        self._open_flow_btn = load_btn
        tb_layout.addWidget(load_btn)

        save_btn = QPushButton("Save")
        save_btn.setProperty("cssClass", "primary")
        save_btn.clicked.connect(self._save_flow)
        tb_layout.addWidget(save_btn)

        more_menu = QMenu(self)
        self._rename_flow_btn = more_menu.addAction("Rename flow…")
        self._rename_flow_btn.setEnabled(False)
        self._rename_flow_btn.triggered.connect(self._rename_flow)
        self._versions_flow_btn = more_menu.addAction("Version history…")
        self._versions_flow_btn.setEnabled(False)
        self._versions_flow_btn.triggered.connect(self._show_flow_versions)
        more_menu.addSeparator()
        import_action = more_menu.addAction("Import flow…")
        import_action.triggered.connect(self._import_flow)
        self._import_flow_action = import_action
        export_action = more_menu.addAction("Export flow…")
        export_action.triggered.connect(self._export_flow)
        more_menu.addSeparator()
        clear_action = more_menu.addAction("Clear commands…")
        clear_action.triggered.connect(self._clear_flow)
        self._clear_flow_action = clear_action
        self._delete_flow_btn = more_menu.addAction("Delete flow…")
        self._delete_flow_btn.setEnabled(False)
        self._delete_flow_btn.triggered.connect(self._delete_flow)
        self._run_step_flow_mutation_controls = [
            self._add_command_btn,
            self._web_inspector_btn,
            self._flow_parameters_btn,
            self._hooks_btn,
            self._new_flow_btn,
            self._open_flow_btn,
            self._rename_flow_btn,
            self._versions_flow_btn,
            self._import_flow_action,
            self._clear_flow_action,
            self._delete_flow_btn,
        ]

        more_btn = QToolButton()
        more_btn.setText("More")
        more_btn.setToolTip("Flow actions")
        more_btn.setMenu(more_menu)
        more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        tb_layout.addWidget(more_btn)

        save_shortcut = QAction(self)
        save_shortcut.setShortcut("Ctrl+S")
        save_shortcut.triggered.connect(self._save_flow)
        self.addAction(save_shortcut)

        parent_layout.addWidget(toolbar)

    def _edit_flow_parameters(self) -> None:
        if self._flow.value_model_version == CURRENT_FLOW_VALUE_MODEL_VERSION:
            dialog = FlowValuesDialog(
                self._flow.inputs,
                self._flow.variables,
                self._flow.outputs,
                self._flow.commands,
                parent=self,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            self._flow.inputs = dialog.inputs()
            self._flow.variables = dialog.variables()
            self._flow.outputs = dialog.outputs()
            self._rebuild_cards()
            self._refresh_document_state()
            return
        dialog = FlowParametersDialog(self._flow.parameters, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._flow.parameters = dialog.parameters()
        self._refresh_document_state()

    def _edit_resolution_scaling(self) -> None:
        dialog = ResolutionScalingDialog(self._flow.resolution_scaling, self)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            self._flow.resolution_scaling = dialog.settings()
            self._refresh_document_state()
        finally:
            dialog.deleteLater()

    def _edit_hooks(self) -> None:
        dialog = HooksManagerDialog(
            self._flow,
            self._command_types,
            self._db,
            self,
            test_condition=self._test_hook_condition,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        removed = dialog.removed_hook_ids()
        self._flow.hooks = dialog.hooks()
        if removed:
            self._remove_hook_references(self._flow.commands, removed)
        self._rebuild_cards()

    @staticmethod
    def _remove_hook_references(
        commands: list[FlowCommand],
        hook_ids: set[str],
    ) -> None:
        for command in commands:
            command.hook_bindings = [
                binding
                for binding in command.hook_bindings
                if binding.hook_id not in hook_ids
            ]
            if command.id == "check_hooks":
                values = command.params.get("hooks")
                if isinstance(values, list):
                    command.params["hooks"] = [
                        value for value in values if value not in hook_ids
                    ]
            FlowEditorView._remove_hook_references(command.children, hook_ids)
            FlowEditorView._remove_hook_references(command.else_children, hook_ids)

    def _create_and_attach_hook(
        self,
        command: FlowCommand,
        hook_type: HookType,
    ) -> None:
        dialog = HookEditDialog(
            self._flow,
            self._command_types,
            self._db,
            hook_type=hook_type,
            test_condition=self._test_hook_condition,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        hook = dialog.hook()
        hook.automatic_scope = HookAutomaticScope.SELECTED_COMMANDS
        self._flow.hooks.append(hook)
        command.hook_bindings.append(
            HookBinding(hook.hook_id, (HookPhase.BEFORE,))
        )
        self._rebuild_cards()

    def _create_flow_variable_for_types(
        self,
        allowed_types: set[ValueType],
    ) -> str | None:
        """Create an output Variable inline and return its stable ID."""
        dialog = FlowValueEditDialog(
            "variable",
            allowed_types=tuple(value_type for value_type in ValueType if value_type in allowed_types),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        definition = dialog.definition()
        try:
            catalog = FlowValueCatalog.build(
                self._flow.inputs,
                [*self._flow.variables, definition],
                self._flow.outputs,
            )
        except FlowValueDeclarationError as exc:
            QMessageBox.warning(self, "Invalid Variable", str(exc))
            return None
        self._flow.variables = list(catalog.variables)
        self._refresh_document_state()
        QTimer.singleShot(0, self._rebuild_cards)
        return definition.id

    # ── Scroll area ─────────────────────────────────────────────────────

    def _build_scroll_area(self, parent_layout: QVBoxLayout) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setStyleSheet(
            f"QSplitter::handle {{ background-color: {COLORS['border']}; }}"
        )

        outline_panel = QWidget()
        outline_panel.setMinimumWidth(280)
        outline_panel.setMaximumWidth(16777215)
        outline_panel.setStyleSheet(
            f"background-color: {COLORS['bg_surface']}; "
            f"border-right: 1px solid {COLORS['border']};"
        )
        outline_layout = QVBoxLayout(outline_panel)
        outline_layout.setContentsMargins(10, 10, 8, 10)
        outline_layout.setSpacing(8)

        outline_header_row = QWidget()
        outline_header_layout = QHBoxLayout(outline_header_row)
        outline_header_layout.setContentsMargins(0, 0, 0, 0)
        outline_header_layout.setSpacing(8)

        outline_header = QLabel("Flow Outline")
        outline_header.setStyleSheet(
            f"color: {COLORS['text_primary']}; font-size: 10.5pt; "
            "font-weight: 800;"
        )
        outline_header_layout.addWidget(outline_header)
        outline_header_layout.addStretch()

        self._outline_drag_toggle = QCheckBox("Drag")
        self._outline_drag_toggle.setToolTip(
            "Enable drag reordering in the outline. Keep off to avoid accidental moves."
        )
        self._outline_drag_toggle.setStyleSheet(
            f"QCheckBox {{ color: {COLORS['text_secondary']}; font-size: 10pt; }}"
            f"QCheckBox::indicator:checked {{ background-color: {COLORS['primary']}; }}"
        )
        outline_header_layout.addWidget(self._outline_drag_toggle)
        outline_layout.addWidget(outline_header_row)

        self._outline_tree = _FlowOutlineTree(self)
        self._outline_tree.setColumnCount(3)
        self._outline_tree.setHeaderLabels(["Step", "Command", "Details"])
        self._outline_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._outline_tree.setIndentation(14)
        self._outline_tree.setRootIsDecorated(True)
        self._outline_tree.setUniformRowHeights(False)
        outline_font = self._outline_tree.font()
        outline_font.setPointSizeF(10.5)
        self._outline_tree.setFont(outline_font)
        header = self._outline_tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        palette = self._outline_tree.palette()
        palette.setColor(QPalette.ColorRole.Base, QColor(COLORS["bg_surface"]))
        palette.setColor(QPalette.ColorRole.Text, QColor(COLORS["text_secondary"]))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(COLORS["primary"]))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(COLORS["text_primary"]))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor(255, 255, 255, 6))
        self._outline_tree.setPalette(palette)
        header_font = header.font()
        header_font.setPointSizeF(10.0)
        header_font.setBold(True)
        header.setFont(header_font)
        self._outline_tree.setAlternatingRowColors(True)
        self._outline_tree.itemClicked.connect(self._on_outline_item_clicked)
        self._outline_drag_toggle.toggled.connect(
            self._outline_tree.set_outline_drag_enabled
        )
        outline_layout.addWidget(self._outline_tree, 1)

        hint = QLabel("Click to jump. Enable Drag only when intentionally reordering.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {COLORS['text_tertiary']}; font-size: 10pt;")
        outline_layout.addWidget(hint)

        splitter.addWidget(outline_panel)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(f"QScrollArea {{ border: none; background: {COLORS['bg_darkest']}; }}")

        self._cards_widget = _DroppableCardsWidget(self)
        self._cards_widget.setStyleSheet(f"background: {COLORS['bg_darkest']};")
        self._cards_layout = QVBoxLayout(self._cards_widget)
        self._cards_layout.setContentsMargins(16, 12, 16, 12)
        self._cards_layout.setSpacing(4)
        self._cards_layout.addStretch()

        self._scroll.setWidget(self._cards_widget)
        splitter.addWidget(self._scroll)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 980])
        parent_layout.addWidget(splitter, 1)

    def _start_drag_auto_scroll(self) -> None:
        if not self._drag_scroll_timer.isActive():
            self._drag_scroll_timer.start()

    def _stop_drag_auto_scroll(self) -> None:
        self._drag_scroll_timer.stop()

    def _auto_scroll_during_drag(self) -> None:
        viewport = self._scroll.viewport()
        pos = viewport.mapFromGlobal(QCursor.pos())
        if not viewport.rect().contains(pos):
            return

        margin = 72
        y = pos.y()
        height = viewport.height()
        delta = 0
        if y < margin:
            delta = -self._edge_scroll_delta(y, margin)
        elif y > height - margin:
            delta = self._edge_scroll_delta(height - y, margin)

        if delta:
            bar = self._scroll.verticalScrollBar()
            bar.setValue(bar.value() + delta)

    @staticmethod
    def _edge_scroll_delta(distance_from_edge: int, margin: int) -> int:
        closeness = max(0.0, min(1.0, (margin - distance_from_edge) / margin))
        return max(4, int(28 * closeness))

    # ── Card rebuild ────────────────────────────────────────────────────

    def _rebuild_cards(self) -> None:
        """Clear and recreate all command cards from the current flow."""
        self._card_by_step_id = {}
        while self._cards_layout.count():
            item = self._cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        for i, cmd in enumerate(self._flow.commands):
            card = CommandCard(
                cmd, self._command_types, self, depth=0, index=i, db=self._db
            )
            self._cards_layout.addWidget(card)

        if not self._flow.commands:
            empty = QWidget()
            empty_layout = QVBoxLayout(empty)
            empty_layout.setContentsMargins(40, 90, 40, 40)
            empty_layout.setSpacing(10)
            title = QLabel("Build your first automation step")
            title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            title.setStyleSheet(
                f"color: {COLORS['text_primary']}; font-size: 15pt; font-weight: 650;"
            )
            detail = QLabel(
                "Add commands, arrange them into branches, then run the flow against the selected device."
            )
            detail.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            detail.setWordWrap(True)
            detail.setStyleSheet(
                f"color: {COLORS['text_secondary']}; font-size: 10.5pt;"
            )
            empty_layout.addWidget(title)
            empty_layout.addWidget(detail)
            empty_layout.addStretch()
            self._cards_layout.addWidget(empty)

        self._cards_layout.addStretch()
        self._refresh_document_state()
        self._rebuild_outline()
        self._apply_selected_card_highlight()

    def _register_command_card(self, step_id: str, card: CommandCard) -> None:
        self._card_by_step_id[step_id] = card

    def _command_section_expanded(
        self,
        step_id: str,
        section_key: str,
        default: bool,
    ) -> bool:
        return self._command_section_states.get(
            (step_id, section_key),
            default,
        )

    def _set_command_section_expanded(
        self,
        step_id: str,
        section_key: str,
        expanded: bool,
    ) -> None:
        self._command_section_states[(step_id, section_key)] = expanded

    def _reset_command_section_states(self) -> None:
        self._command_section_states.clear()

    # ── Flow outline ────────────────────────────────────────────────────

    def _rebuild_outline(self) -> None:
        if not hasattr(self, "_outline_tree"):
            return

        self._outline_tree.blockSignals(True)
        self._outline_tree.clear()
        self._outline_item_by_step_id = {}

        if not self._flow.commands:
            empty_item = QTreeWidgetItem(["", "No commands", ""])
            empty_item.setFlags(empty_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            empty_item.setForeground(1, QBrush(QColor(COLORS["text_tertiary"])))
            self._outline_tree.addTopLevelItem(empty_item)
        else:
            for index, cmd in enumerate(self._flow.commands, start=1):
                self._add_outline_command(None, cmd, str(index))

        self._outline_tree.expandAll()
        if self._selected_step_id:
            item = self._outline_item_by_step_id.get(self._selected_step_id)
            if item is not None:
                hbar = self._outline_tree.horizontalScrollBar()
                hvalue = hbar.value()
                self._outline_tree.setCurrentItem(item)
                hbar.setValue(hvalue)
        self._outline_tree.blockSignals(False)

    def _add_outline_command(
        self,
        parent_item: QTreeWidgetItem | None,
        cmd: FlowCommand,
        path: str,
    ) -> QTreeWidgetItem:
        label = self._command_types.get(cmd.id, {}).get("label", cmd.id)
        details = self._outline_command_details(cmd)
        item = QTreeWidgetItem([path, label, details])
        item.setData(0, Qt.ItemDataRole.UserRole, cmd.step_id)
        item.setToolTip(0, self._outline_command_tooltip(cmd))
        item.setToolTip(1, self._outline_command_tooltip(cmd))
        item.setToolTip(2, details)
        self._apply_outline_command_style(item, cmd)
        if not cmd.enabled:
            for column in range(3):
                item.setForeground(column, QBrush(QColor(COLORS["text_tertiary"])))

        if parent_item is None:
            self._outline_tree.addTopLevelItem(item)
        else:
            parent_item.addChild(item)
        self._outline_item_by_step_id[cmd.step_id] = item

        if cmd.id == "switch_value":
            case_number = 0
            for branch in cmd.children:
                if branch.id == "switch_case":
                    case_number += 1
                    branch_label = f"Case {case_number}"
                    branch_path = f"{path}c{case_number}"
                else:
                    branch_label = "Default"
                    branch_path = f"{path}d"
                branch_item = QTreeWidgetItem(
                    ["", branch_label, f"{len(branch.children)} step(s)"]
                )
                branch_item.setData(0, Qt.ItemDataRole.UserRole, branch.step_id)
                branch_item.setForeground(
                    1,
                    QBrush(
                        QColor(
                            COLORS["orange"]
                            if branch.id == "switch_default"
                            else COLORS["primary"]
                        )
                    ),
                )
                self._apply_outline_branch_font(branch_item)
                item.addChild(branch_item)
                self._outline_item_by_step_id[branch.step_id] = branch_item
                for child_index, child in enumerate(branch.children, start=1):
                    self._add_outline_command(
                        branch_item,
                        child,
                        f"{branch_path}.{child_index}",
                    )
            return item

        if cmd.children:
            then_item = QTreeWidgetItem(["", "Then", f"{len(cmd.children)} step(s)"])
            then_item.setFlags(then_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            for column in range(3):
                then_item.setForeground(column, QBrush(QColor(COLORS["text_tertiary"])))
            self._apply_outline_branch_font(then_item)
            item.addChild(then_item)
            for index, child in enumerate(cmd.children, start=1):
                self._add_outline_command(then_item, child, f"{path}.{index}")

        if cmd.else_children:
            else_item = QTreeWidgetItem(["", "Else", f"{len(cmd.else_children)} step(s)"])
            else_item.setFlags(else_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            for column in range(3):
                else_item.setForeground(column, QBrush(QColor(COLORS["orange"])))
            self._apply_outline_branch_font(else_item)
            item.addChild(else_item)
            for index, child in enumerate(cmd.else_children, start=1):
                self._add_outline_command(else_item, child, f"{path}e{index}")

        return item

    def _outline_command_details(self, cmd: FlowCommand) -> str:
        badges = self._outline_badges(cmd)
        return " | ".join(badges)

    def _apply_outline_command_style(
        self,
        item: QTreeWidgetItem,
        cmd: FlowCommand,
    ) -> None:
        category = self._command_types.get(cmd.id, {}).get("category", "")
        category_color = CATEGORY_COLORS.get(category, COLORS["primary"])
        command_bg = QColor(category_color)
        command_bg.setAlpha(90)
        command_fg = QColor(COLORS["text_primary"])

        item.setForeground(0, QBrush(QColor(COLORS["text_tertiary"])))
        item.setForeground(1, QBrush(command_fg))
        item.setForeground(2, QBrush(QColor(COLORS["text_secondary"])))
        item.setBackground(1, QBrush(command_bg))

        step_font = QFont()
        step_font.setPointSizeF(10.0)
        item.setFont(0, step_font)

        command_font = QFont()
        command_font.setPointSizeF(10.75)
        command_font.setBold(True)
        item.setFont(1, command_font)

        details_font = QFont()
        details_font.setPointSizeF(10.25)
        item.setFont(2, details_font)

        params = cmd.params
        if self._is_visual_image_command(cmd) and params.get("color_match") is False:
            item.setForeground(2, QBrush(QColor(COLORS["warning"])))
        if params.get("click_if_found") or params.get("clickIfFound"):
            item.setBackground(2, QBrush(QColor(63, 185, 80, 32)))
        if cmd.breakpoint:
            item.setBackground(0, QBrush(QColor(210, 153, 34, 42)))

    @staticmethod
    def _apply_outline_branch_font(item: QTreeWidgetItem) -> None:
        branch_font = QFont()
        branch_font.setPointSizeF(10.0)
        branch_font.setBold(True)
        for column in range(3):
            item.setFont(column, branch_font)

    def _outline_command_tooltip(self, cmd: FlowCommand) -> str:
        label = self._command_types.get(cmd.id, {}).get("label", cmd.id)
        details = [label, f"step: {cmd.step_id[:8]}"]
        if cmd.comment:
            details.append(f"note: {cmd.comment}")
        badges = self._outline_badges(cmd, include_labels=True)
        if badges:
            details.append(", ".join(badges))
        return "\n".join(details)

    def _outline_badges(
        self,
        cmd: FlowCommand,
        *,
        include_labels: bool = False,
    ) -> list[str]:
        params = cmd.params
        badges: list[str] = []

        images = params.get("images") or params.get("image_name")
        if isinstance(images, list):
            visible = [str(name) for name in images if name][:2]
            if visible:
                extra = len(images) - len(visible)
                text = ", ".join(visible)
                if extra > 0:
                    text += f" +{extra}"
                badges.append(text)
        elif images:
            badges.append(str(images))

        for name in ("condition", "mode", "click"):
            value = params.get(name)
            if value not in (None, "", False):
                badges.append(f"{name}: {value}" if include_labels else str(value))

        if params.get("click_if_found") or params.get("clickIfFound"):
            badges.append("click if found" if include_labels else "click")

        threshold = params.get("threshold")
        if threshold not in (None, ""):
            badges.append(f"threshold {threshold}" if include_labels else f"thr {threshold}")

        if self._is_visual_image_command(cmd):
            color_match = params.get("color_match")
            if color_match is True:
                tolerance = params.get("color_tolerance")
                if tolerance not in (None, ""):
                    badges.append(
                        f"color tolerance {tolerance}" if include_labels else f"color {tolerance}"
                    )
                else:
                    badges.append("color match" if include_labels else "color")
            elif color_match is False:
                badges.append("color match disabled" if include_labels else "no-color")

        if not cmd.enabled:
            badges.append("disabled")
        if cmd.on_error != "stop":
            badges.append(f"on error: {cmd.on_error}" if include_labels else cmd.on_error)
        if cmd.breakpoint:
            badges.append("breakpoint")
        return badges

    @staticmethod
    def _is_visual_image_command(cmd: FlowCommand) -> bool:
        if "image" in cmd.id:
            return True
        params = cmd.params
        return bool(params.get("images") or params.get("image_name"))

    def _on_outline_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        step_id = item.data(0, Qt.ItemDataRole.UserRole)
        if step_id:
            self._select_command_step(str(step_id), scroll=True)

    def _select_command_step(self, step_id: str, *, scroll: bool = True) -> None:
        self._selected_step_id = step_id
        self._apply_selected_card_highlight()

        item = self._outline_item_by_step_id.get(step_id)
        if item is not None and self._outline_tree.currentItem() is not item:
            hbar = self._outline_tree.horizontalScrollBar()
            hvalue = hbar.value()
            self._outline_tree.blockSignals(True)
            self._outline_tree.setCurrentItem(item)
            self._outline_tree.blockSignals(False)
            hbar.setValue(hvalue)

        card = self._card_by_step_id.get(step_id)
        if card is not None:
            if scroll:
                self._scroll.ensureWidgetVisible(card, 24, 24)
                self._scroll.horizontalScrollBar().setValue(0)
            card.setFocus(Qt.FocusReason.OtherFocusReason)

    def _apply_selected_card_highlight(self) -> None:
        for step_id, card in self._card_by_step_id.items():
            if hasattr(card, "set_outline_selected"):
                card.set_outline_selected(step_id == self._selected_step_id)

    def _on_command_metadata_changed(self, _command: FlowCommand) -> None:
        self._rebuild_outline()

    def _launch_android_package_picker(
        self,
        command: FlowCommand,
        param_name: str,
    ) -> None:
        """Choose a package from the currently connected Android device."""
        session = (
            self._device_selector.get_selected_session()
            if self._device_selector is not None
            else None
        )
        from automadin.models.device import AndroidDevice

        if session is None or not isinstance(session.device, AndroidDevice):
            QMessageBox.information(
                self,
                "Connect an Android Device",
                "Connect an Android device in the target selector before "
                "browsing installed packages.",
            )
            return

        from automadin.ui.flow_editor.android_package_picker import (
            AndroidPackagePickerDialog,
        )

        dialog = AndroidPackagePickerDialog(
            lambda: self._session_service.list_android_packages(session),
            current_value=str(command.params.get(param_name, "")),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        command.params[param_name] = dialog.selected_package
        self._rebuild_cards()

    def _launch_capture_target(
        self,
        command: FlowCommand,
        param_name: str,
    ) -> None:
        """Stage and atomically apply one captured image-list target."""
        if param_name != "images":
            return
        if self._capture_controller is None:
            QMessageBox.information(
                self,
                "Capture Unavailable",
                "Screenshot capture is unavailable in this window.",
            )
            return

        from automadin.ui.flow_editor.capture_target_dialog import (
            CaptureTargetDialog,
        )

        command_definition = self._command_types.get(command.id, {})
        supported_params = {
            str(param.get("name", ""))
            for param in command_definition.get("params", [])
            if param.get("name")
        }
        current_images = command.params.get(param_name)
        suggested_group = ""
        if isinstance(current_images, list) and current_images:
            suggested_group = resource_group_path(str(current_images[-1]))
        elif self._flow.name and self._flow.name != "Untitled":
            suggested_group = resource_group_path(self._flow.name)

        dialog = CaptureTargetDialog(
            self._db,
            device_selector=self._device_selector,
            capture_controller=self._capture_controller,
            supported_params=supported_params,
            current_params=command.params,
            suggested_group=suggested_group,
            current_document_name=self._flow.name,
            current_document_images=self._current_document_image_names(),
            parent=self,
        )
        try:
            while dialog.exec() == QDialog.DialogCode.Accepted:
                draft = dialog.draft
                if draft is None:
                    dialog.set_commit_error(
                        "The captured target was incomplete. Review it and try again."
                    )
                    continue

                before_params = copy.deepcopy(command.params)
                after_params = copy.deepcopy(command.params)
                after_params[param_name] = self._append_canonical_image(
                    after_params.get(param_name),
                    draft.name,
                )
                after_params.update(
                    {
                        name: value
                        for name, value in draft.updates_dict().items()
                        if name in supported_params
                    }
                )

                # Mutate the in-memory document first. This operation is
                # deliberately simple and reversible; the guarded database
                # write then either commits the matching asset revision or
                # restores the exact previous command snapshot.
                command.params.clear()
                command.params.update(after_params)
                try:
                    if draft.replace_revision is None:
                        self._db.create_image(
                            draft.name,
                            draft.png_bytes,
                            draft.width,
                            draft.height,
                            source_surface_width=draft.source_surface_width,
                            source_surface_height=draft.source_surface_height,
                            source_coordinate_space=draft.source_coordinate_space,
                        )
                    else:
                        self._db.replace_image_if_revision(
                            draft.replace_revision,
                            draft.png_bytes,
                            draft.width,
                            draft.height,
                            source_surface_width=draft.source_surface_width,
                            source_surface_height=draft.source_surface_height,
                            source_coordinate_space=draft.source_coordinate_space,
                        )
                except (ImageAlreadyExistsError, ImageRevisionConflictError):
                    command.params.clear()
                    command.params.update(before_params)
                    self._on_command_metadata_changed(command)
                    dialog.set_commit_error(
                        "The image library changed after it was reviewed. "
                        "Review the name and try again."
                    )
                    continue
                except Exception:
                    command.params.clear()
                    command.params.update(before_params)
                    self._on_command_metadata_changed(command)
                    dialog.set_commit_error(
                        "The image could not be saved. Your flow and existing "
                        "images were not changed. Try again."
                    )
                    continue

                self._on_command_metadata_changed(command)
                self.resource_saved.emit("image", draft.name)
                self._rebuild_cards()
                return
        finally:
            dialog.deleteLater()

    def _launch_dynamic_target_recorder(
        self,
        command: FlowCommand,
        param_name: str,
    ) -> None:
        """Record and atomically attach one animated target pose set."""
        if command.id != "click_dynamic_target" or param_name != "images":
            return
        if self._capture_controller is None:
            QMessageBox.information(
                self,
                "Recording Unavailable",
                "Screenshot capture is unavailable in this window.",
            )
            return
        from automadin.ui.flow_editor.dynamic_target_recorder import (
            DynamicTargetRecorderDialog,
        )

        suggested_name = "Target"
        current_images = command.params.get("images")
        if isinstance(current_images, list) and current_images:
            suggested_name = resource_display_label(resource_group_path(current_images[0]))
        dialog = DynamicTargetRecorderDialog(
            self._db,
            device_selector=self._device_selector,
            capture_controller=self._capture_controller,
            suggested_name=suggested_name,
            parent=self,
        )
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted or dialog.draft is None:
                return
            draft = dialog.draft
            before_params = copy.deepcopy(command.params)
            command.params.update(draft.updates_dict())
            images = [
                StoredImage(
                    pose.name,
                    pose.png_bytes,
                    pose.width,
                    pose.height,
                    draft.source_surface_width,
                    draft.source_surface_height,
                    draft.source_coordinate_space,
                )
                for pose in draft.poses
            ]
            try:
                self._db.create_images(images)
            except ImageAlreadyExistsError:
                command.params.clear()
                command.params.update(before_params)
                QMessageBox.warning(
                    self,
                    "Target Name Changed",
                    "An image with one of the learned pose names now exists. "
                    "Record again with another target name.",
                )
                return
            except Exception:
                command.params.clear()
                command.params.update(before_params)
                QMessageBox.warning(
                    self,
                    "Target Could Not Be Saved",
                    "No pose images were saved and the command was restored.",
                )
                return
            self._on_command_metadata_changed(command)
            for pose in draft.poses:
                self.resource_saved.emit("image", pose.name)
            self._rebuild_cards()
        finally:
            dialog.deleteLater()

    def _launch_visual_rule_editor(
        self,
        command: FlowCommand,
        param_name: str,
        current_name: str,
    ) -> str | None:
        """Create, edit, calibrate, and test a rule without leaving the command."""
        from automadin.ui.visual_rule_dialog import VisualRuleDialog

        existing = self._db.load_visual_rule(current_name) if current_name else None
        dialog = VisualRuleDialog(
            existing=existing,
            session_service=self._session_service,
            device_selector=self._device_selector,
            known_paths=self._db.list_resource_paths(),
            known_folders=self._db.list_resource_folders(),
            capture_controller=self._capture_controller,
            parent=self,
        )
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return None
            updated = dialog.get_rule()
        finally:
            dialog.deleteLater()
        if updated is None or not updated.name:
            QMessageBox.warning(self, "Name Required", "Enter a visual rule name.")
            return None
        collision = find_resource_collision(
            updated.name,
            self._db.list_visual_rules(),
            ignore_path=current_name,
        )
        if collision is not None:
            QMessageBox.warning(self, "Name Taken", f"'{collision}' already exists.")
            return None
        if existing is not None and updated.name != current_name:
            if not self._db.rename_visual_rule(current_name, updated.name):
                QMessageBox.warning(
                    self,
                    "Visual Rule Changed",
                    "The selected rule no longer exists. Reopen the editor and try again.",
                )
                return None
            self.resource_renamed.emit("visual_rule", current_name, updated.name)
        self._db.save_visual_rule(updated)
        command.params[param_name] = updated.name
        self._on_command_metadata_changed(command)
        self.resource_saved.emit("visual_rule", updated.name)
        return updated.name

    def preview_screen_text(self, command: FlowCommand) -> None:
        """Open a side-effect-free OCR preview for one command snapshot."""
        if self._capture_controller is None:
            QMessageBox.information(
                self,
                "Preview Unavailable",
                "Screenshot capture is unavailable in this window.",
            )
            return
        if self._device_selector is None:
            QMessageBox.information(
                self,
                "Connect a Target",
                "Connect a target before previewing screen text.",
            )
            return
        from automadin.ui.flow_editor.screen_text_preview_dialog import (
            ScreenTextPreviewDialog,
        )

        dialog = ScreenTextPreviewDialog(
            command,
            self._db,
            device_selector=self._device_selector,
            capture_controller=self._capture_controller,
            parent=self,
        )
        dialog.exec()
        dialog.deleteLater()

    def preview_image_search(self, command: FlowCommand) -> None:
        """Open a side-effect-free scoped image-search preview."""
        if self._capture_controller is None:
            QMessageBox.information(
                self,
                "Preview Unavailable",
                "Screenshot capture is unavailable in this window.",
            )
            return
        if self._device_selector is None:
            QMessageBox.information(
                self,
                "Connect a Target",
                "Connect a target before previewing image search.",
            )
            return
        from automadin.ui.flow_editor.image_search_preview_dialog import (
            ImageSearchPreviewDialog,
        )

        dialog = ImageSearchPreviewDialog(
            command,
            self._db,
            device_selector=self._device_selector,
            capture_controller=self._capture_controller,
            parent=self,
        )
        dialog.exec()
        dialog.deleteLater()

    def preview_dynamic_target(self, command: FlowCommand) -> None:
        """Run the complete detector/tracker strategy without sending input."""
        preview = FlowCommand.from_dict(command.to_dict())
        preview.params["preview_only"] = True
        self.run_single_command(preview)

    @staticmethod
    def _append_canonical_image(value: Any, name: str) -> list[str]:
        """Append once while preserving priority and repairing equivalent casing."""
        existing = value if isinstance(value, list) else []
        target = normalize_resource_path(name).casefold()
        result: list[str] = []
        found = False
        for item in existing:
            text = str(item)
            if normalize_resource_path(text).casefold() == target:
                if not found:
                    result.append(name)
                    found = True
                continue
            result.append(text)
        if not found:
            result.append(name)
        return result

    def _current_document_image_names(self) -> set[str]:
        names: set[str] = set()

        def collect(commands: list[FlowCommand]) -> None:
            for item in commands:
                scalar = item.params.get("image_name")
                if isinstance(scalar, str) and scalar:
                    names.add(scalar)
                images = item.params.get("images")
                if isinstance(images, list):
                    names.update(str(name) for name in images if name)
                collect(item.children)
                collect(item.else_children)

        collect(self._flow.commands)
        return names

    # ── Command operations ──────────────────────────────────────────────

    def _make_command(self, cmd_id: str) -> FlowCommand:
        """Create a new FlowCommand with default param values."""
        cmd_def = self._command_types.get(cmd_id, {})
        command = FlowCommand(id=cmd_id, params=get_default_params(cmd_def))
        if cmd_id == "switch_case":
            command.collapsed = False
        if cmd_id == "switch_value":
            command.children = [
                self._make_command("switch_case"),
                self._make_command("switch_default"),
            ]
        return command

    def _add_switch_case(self, switch: FlowCommand) -> FlowCommand:
        """Insert a new Case immediately before a Switch's permanent Default."""
        case = self._make_command("switch_case")
        default_index = next(
            (
                index
                for index, branch in enumerate(switch.children)
                if branch.id == "switch_default"
            ),
            len(switch.children),
        )
        switch.children.insert(default_index, case)
        self._rebuild_cards()
        return case

    def _remove_switch_case(self, case: FlowCommand) -> None:
        """Remove one Case while retaining at least one Case in its Switch."""
        switch = self._find_parent_command(self._flow.commands, case)
        if switch is None or switch.id != "switch_value":
            return
        cases = [branch for branch in switch.children if branch.id == "switch_case"]
        if len(cases) <= 1:
            return
        switch.children.remove(case)
        self._rebuild_cards()

    def _duplicate_switch_case(self, case: FlowCommand) -> FlowCommand | None:
        """Duplicate one Case, including its commands, immediately below it."""
        switch = self._find_parent_command(self._flow.commands, case)
        if switch is None or switch.id != "switch_value" or case.id != "switch_case":
            return None
        try:
            case_index = switch.children.index(case)
        except ValueError:
            return None
        duplicate = case.deep_copy()
        duplicate.collapsed = False
        switch.children.insert(case_index + 1, duplicate)
        self._rebuild_cards()
        return duplicate

    def _move_switch_case(self, case: FlowCommand, direction: int) -> None:
        """Move a Case among sibling Cases without crossing the final Default."""
        switch = self._find_parent_command(self._flow.commands, case)
        if switch is None or switch.id != "switch_value":
            return
        case_indexes = [
            index
            for index, branch in enumerate(switch.children)
            if branch.id == "switch_case"
        ]
        try:
            position = case_indexes.index(switch.children.index(case))
        except ValueError:
            return
        target_position = position + direction
        if not 0 <= target_position < len(case_indexes):
            return
        source_index = case_indexes[position]
        target_index = case_indexes[target_position]
        switch.children[source_index], switch.children[target_index] = (
            switch.children[target_index],
            switch.children[source_index],
        )
        self._rebuild_cards()

    def _add_command(self) -> None:
        dlg = CommandPaletteDialog(self._command_types, self)
        if dlg.exec() and dlg.get_selected():
            cmd = self._make_command(dlg.get_selected())
            self._flow.commands.append(cmd)
            self._rebuild_cards()

    def _open_web_inspector(self) -> None:
        """Open one modeless inspector for the connected interactive browser."""
        if self._web_inspector_dialog is not None:
            self._web_inspector_dialog.show()
            self._web_inspector_dialog.raise_()
            self._web_inspector_dialog.activateWindow()
            return
        session = (
            self._device_selector.get_selected_session()
            if self._device_selector is not None
            else None
        )
        from automadin.engine.browser_session import BrowserSessionHandle
        from automadin.models.device import BrowserDevice

        handle = session.get_data("browser_data") if session is not None else None
        if (
            session is None
            or not isinstance(session.device, BrowserDevice)
            or not isinstance(handle, BrowserSessionHandle)
        ):
            QMessageBox.information(
                self,
                "Connect a Browser",
                "Connect a headed browser in the target selector before opening "
                "the Web Inspector.",
            )
            return

        from automadin.ui.flow_editor.web_inspector_dialog import WebInspectorDialog

        dialog = WebInspectorDialog(
            handle,
            self,
            locator_actions=self._web_inspector_locator_actions(),
        )
        dialog.use_requested.connect(self._add_inspected_locator_step)
        dialog.destroyed.connect(self._on_web_inspector_destroyed)
        self._web_inspector_dialog = dialog
        dialog.show()

    def _web_inspector_locator_actions(self) -> tuple[tuple[str, str], ...]:
        """Return ordinary commands that accept the inspector's primary locator."""
        preferred_order = (
            "click_element",
            "scroll_browser",
            "wait_for_selector",
            "input_fill",
            "extract_text",
            "assert_visible",
        )
        supported: dict[str, str] = {}
        for command_id, definition in self._command_types.items():
            if definition.get("category") != "BROWSER":
                continue
            supports_locator = any(
                parameter.get("name") == "locator"
                and parameter.get("type") == "locator"
                for parameter in definition.get("params", ())
                if isinstance(parameter, dict)
            )
            if supports_locator:
                supported[command_id] = str(definition.get("label") or command_id)
        ordered_ids = [
            command_id for command_id in preferred_order if command_id in supported
        ]
        ordered_ids.extend(
            sorted(
                supported.keys() - set(ordered_ids),
                key=lambda command_id: supported[command_id].casefold(),
            )
        )
        actions = [(command_id, supported[command_id]) for command_id in ordered_ids]
        return tuple(actions)

    def _add_inspected_locator_step(
        self,
        command_id: str,
        locator: dict[str, object],
    ) -> None:
        allowed = dict(self._web_inspector_locator_actions())
        allowed["for_each_browser_element"] = "For Each Browser Element"
        if command_id not in allowed:
            return
        command = self._make_command(command_id)
        command.params["locator"] = dict(locator)
        self._flow.commands.append(command)
        self._rebuild_cards()
        self._select_command_step(command.step_id, scroll=True)

    def _on_web_inspector_destroyed(self, *_args: object) -> None:
        self._web_inspector_dialog = None

    def _add_child_command(self, parent_cmd: FlowCommand, target: str) -> None:
        """Add a new command as a child (or else_child) of parent_cmd."""
        dlg = CommandPaletteDialog(self._command_types, self)
        if dlg.exec() and dlg.get_selected():
            cmd = self._make_command(dlg.get_selected())
            child_list = getattr(parent_cmd, target)
            child_list.append(cmd)
            self._rebuild_cards()

    def _insert_command_at(self, index: int, command: FlowCommand) -> None:
        self._flow.commands.insert(index, command)
        self._rebuild_cards()

    def _insert_command_above(self, ref_command: FlowCommand) -> None:
        """Open palette and insert new command above ref_command in its parent list."""
        parent_list = self._find_parent_list(ref_command)
        if parent_list is None:
            return
        idx = self._index_of(parent_list, ref_command)
        if idx < 0:
            return
        dlg = CommandPaletteDialog(self._command_types, self)
        if dlg.exec() and dlg.get_selected():
            cmd = self._make_command(dlg.get_selected())
            parent_list.insert(idx, cmd)
            self._rebuild_cards()

    def _duplicate_command(self, command: FlowCommand) -> None:
        parent_list = self._find_parent_list(command)
        if parent_list is None:
            return
        idx = self._index_of(parent_list, command)
        if idx >= 0:
            parent_list.insert(idx + 1, command.deep_copy())
            self._rebuild_cards()

    def _remove_command(self, command: FlowCommand) -> None:
        if self._remove_from_list(self._flow.commands, command):
            self._rebuild_cards()

    def _move_command(self, command: FlowCommand, direction: int) -> None:
        parent_list = self._find_parent_list(command)
        if parent_list is None:
            return
        idx = self._index_of(parent_list, command)
        if idx < 0:
            return
        new_idx = idx + direction
        if 0 <= new_idx < len(parent_list):
            parent_list[idx], parent_list[new_idx] = parent_list[new_idx], parent_list[idx]
            self._rebuild_cards()

    def _move_command_to_parent(
        self, step_id: str, new_parent: FlowCommand
    ) -> None:
        """Move a command (by step_id) out of its current list into new_parent.children."""
        self._move_command_to_branch(step_id, new_parent, "children")

    def _move_command_to_branch(
        self, step_id: str, new_parent: FlowCommand, branch_attr: str
    ) -> None:
        """Move a command to the end of a parent's children or else_children list."""
        cmd = self._find_by_step_id(self._flow.commands, step_id)
        if cmd is None:
            return
        if new_parent.id == "switch_value":
            return
        if cmd.id in {"switch_case", "switch_default"}:
            return
        # Don't allow moving a parent into its own descendant
        if self._is_descendant_of(cmd, new_parent):
            return
        self._remove_from_list(self._flow.commands, cmd)
        getattr(new_parent, branch_attr).append(cmd)
        self._rebuild_cards()

    def _show_move_to_dialog(self, command: FlowCommand) -> None:
        """Open a tree chooser for moving command into root/branch destinations."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Move Command To")
        dlg.resize(480, 520)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(QLabel("Select destination:"))

        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        tree.setIndentation(18)
        tree.setRootIsDecorated(True)
        layout.addWidget(tree, 1)

        root_item = QTreeWidgetItem(["Root (end)"])
        root_item.setData(0, Qt.ItemDataRole.UserRole, ("root", "", ""))
        tree.addTopLevelItem(root_item)

        def command_label(cmd: FlowCommand) -> str:
            cmd_def = self._command_types.get(cmd.id, {})
            return cmd_def.get("label", cmd.id)

        def add_command_node(parent_item: QTreeWidgetItem, cmd: FlowCommand) -> None:
            if cmd.step_id == command.step_id or self._is_descendant_of(command, cmd):
                return

            cmd_def = self._command_types.get(cmd.id, {})
            can_have_children = cmd_def.get("canHaveChildren", False)
            can_have_else = cmd_def.get("canHaveElse", False)
            if not can_have_children and not cmd.children and not cmd.else_children:
                return

            cmd_item = QTreeWidgetItem([command_label(cmd)])
            cmd_item.setFlags(cmd_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            parent_item.addChild(cmd_item)

            if cmd.id == "switch_value":
                case_number = 0
                for branch in cmd.children:
                    if branch.id == "switch_case":
                        case_number += 1
                        branch_label = f"Case {case_number} (end)"
                    else:
                        branch_label = "Default (end)"
                    branch_item = QTreeWidgetItem([branch_label])
                    branch_item.setData(
                        0,
                        Qt.ItemDataRole.UserRole,
                        ("branch", branch.step_id, "children"),
                    )
                    cmd_item.addChild(branch_item)
                    for child in branch.children:
                        add_command_node(branch_item, child)
                return

            if can_have_children:
                then_item = QTreeWidgetItem(["Then (end)"])
                then_item.setData(
                    0,
                    Qt.ItemDataRole.UserRole,
                    ("branch", cmd.step_id, "children"),
                )
                cmd_item.addChild(then_item)
                for child in cmd.children:
                    add_command_node(then_item, child)

            if can_have_else:
                else_item = QTreeWidgetItem(["Else (end)"])
                else_item.setData(
                    0,
                    Qt.ItemDataRole.UserRole,
                    ("branch", cmd.step_id, "else_children"),
                )
                cmd_item.addChild(else_item)
                for child in cmd.else_children:
                    add_command_node(else_item, child)

        for root_cmd in self._flow.commands:
            add_command_node(root_item, root_cmd)

        tree.expandAll()
        tree.setCurrentItem(root_item)

        def accept_destination(item: QTreeWidgetItem, _col: int) -> None:
            if item.data(0, Qt.ItemDataRole.UserRole):
                dlg.accept()

        tree.itemDoubleClicked.connect(accept_destination)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        item = tree.currentItem()
        if item is None:
            return
        target = item.data(0, Qt.ItemDataRole.UserRole)
        if not target:
            return
        kind, parent_step_id, branch_attr = target
        if kind == "root":
            self._move_command_to_root_end(command.step_id)
            return
        parent_cmd = self._find_by_step_id(self._flow.commands, parent_step_id)
        if parent_cmd is not None:
            self._move_command_to_branch(command.step_id, parent_cmd, branch_attr)

    def _move_command_relative(
        self, step_id: str, ref_command: FlowCommand, position: str
    ) -> None:
        """Move a command (by step_id) above or below ref_command in ref's parent list.

        Args:
            step_id: The step_id of the command to move.
            ref_command: The reference command to place relative to.
            position: "above" or "below".
        """
        cmd = self._find_by_step_id(self._flow.commands, step_id)
        if cmd is None or cmd.step_id == ref_command.step_id:
            return
        if cmd.id == "switch_default":
            return
        if cmd.id == "switch_case":
            source_switch = self._find_parent_command(self._flow.commands, cmd)
            target_switch = self._find_parent_command(
                self._flow.commands,
                ref_command,
            )
            if (
                source_switch is None
                or source_switch is not target_switch
                or source_switch.id != "switch_value"
                or ref_command.id not in {"switch_case", "switch_default"}
            ):
                return

        # Don't allow moving a parent into a position inside its own subtree
        if self._is_descendant_of(cmd, ref_command):
            return

        # Find the list that contains ref_command
        parent_list = self._find_parent_list(ref_command)
        if parent_list is None:
            return

        # Remove cmd from its current location
        self._remove_from_list(self._flow.commands, cmd)

        # Re-find index of ref_command (may have shifted after removal)
        ref_idx = self._index_of(parent_list, ref_command)
        if ref_idx < 0:
            # Fallback: just append to root
            self._flow.commands.append(cmd)
        else:
            insert_idx = ref_idx if position == "above" else ref_idx + 1
            parent_list.insert(insert_idx, cmd)

        self._rebuild_cards()

    def _move_command_to_root_end(self, step_id: str) -> None:
        """Move a command to the end of the root command list."""
        cmd = self._find_by_step_id(self._flow.commands, step_id)
        if cmd is None:
            return
        if cmd.id in {"switch_case", "switch_default"}:
            return
        self._remove_from_list(self._flow.commands, cmd)
        self._flow.commands.append(cmd)
        self._rebuild_cards()

    def _move_out_of_parent(self, command: FlowCommand) -> None:
        """Move a command out of its parent container to the parent's level.

        The command is placed immediately after its parent in the parent's list.
        """
        # Find the parent command that contains this command
        parent_cmd = self._find_parent_command(self._flow.commands, command)
        if parent_cmd is None:
            return  # already at root level

        if command.id in {"switch_case", "switch_default"}:
            return

        # Remove from current parent's children/else_children
        removed = False
        idx = self._index_of(parent_cmd.children, command)
        if idx >= 0:
            parent_cmd.children.pop(idx)
            removed = True
        if not removed:
            idx = self._index_of(parent_cmd.else_children, command)
            if idx >= 0:
                parent_cmd.else_children.pop(idx)
                removed = True

        if not removed:
            return

        # Structural Switch branches are not executable siblings. Moving one
        # of their child commands out skips the wrapper and exits the Switch.
        if parent_cmd.id in {"switch_case", "switch_default"}:
            switch = self._find_parent_command(self._flow.commands, parent_cmd)
            if switch is None or switch.id != "switch_value":
                return
            switch_parent_list = self._find_parent_list(switch)
            if switch_parent_list is None:
                return
            switch_index = self._index_of(switch_parent_list, switch)
            switch_parent_list.insert(switch_index + 1, command)
            self._rebuild_cards()
            return

        # Find the list that contains the parent command, and insert after it
        grandparent_list = self._find_parent_list(parent_cmd)
        if grandparent_list is None:
            # Parent is at root — shouldn't happen but fallback
            self._flow.commands.append(command)
        else:
            parent_idx = self._index_of(grandparent_list, parent_cmd)
            if parent_idx >= 0:
                grandparent_list.insert(parent_idx + 1, command)
            else:
                grandparent_list.append(command)

        self._rebuild_cards()

    # ── Tree search helpers ─────────────────────────────────────────────

    def _index_of(self, cmd_list: list[FlowCommand], target: FlowCommand) -> int:
        for i, c in enumerate(cmd_list):
            if c.step_id == target.step_id:
                return i
        return -1

    def _find_parent_list(self, target: FlowCommand) -> list[FlowCommand] | None:
        """Find the list (commands, children, or else_children) that contains target."""
        return self._search_parent_list(self._flow.commands, target)

    def _search_parent_list(
        self, cmd_list: list[FlowCommand], target: FlowCommand
    ) -> list[FlowCommand] | None:
        for c in cmd_list:
            if c.step_id == target.step_id:
                return cmd_list
        for c in cmd_list:
            result = self._search_parent_list(c.children, target)
            if result is not None:
                return result
            result = self._search_parent_list(c.else_children, target)
            if result is not None:
                return result
        return None

    def _remove_from_list(
        self, cmd_list: list[FlowCommand], target: FlowCommand
    ) -> bool:
        for i, c in enumerate(cmd_list):
            if c.step_id == target.step_id:
                cmd_list.pop(i)
                return True
        for c in cmd_list:
            if self._remove_from_list(c.children, target):
                return True
            if self._remove_from_list(c.else_children, target):
                return True
        return False

    def _find_by_step_id(
        self, cmd_list: list[FlowCommand], step_id: str
    ) -> FlowCommand | None:
        for c in cmd_list:
            if c.step_id == step_id:
                return c
            found = self._find_by_step_id(c.children, step_id)
            if found:
                return found
            found = self._find_by_step_id(c.else_children, step_id)
            if found:
                return found
        return None

    def _find_parent_command(
        self, cmd_list: list[FlowCommand], target: FlowCommand
    ) -> FlowCommand | None:
        """Find the parent command that contains target in its children or else_children."""
        for c in cmd_list:
            for child in c.children:
                if child.step_id == target.step_id:
                    return c
            for child in c.else_children:
                if child.step_id == target.step_id:
                    return c
            # Recurse into children
            found = self._find_parent_command(c.children, target)
            if found:
                return found
            found = self._find_parent_command(c.else_children, target)
            if found:
                return found
        return None

    def _is_descendant_of(self, ancestor: FlowCommand, target: FlowCommand) -> bool:
        """Check if target is a descendant of ancestor (preventing circular moves)."""
        for c in ancestor.children:
            if c.step_id == target.step_id:
                return True
            if self._is_descendant_of(c, target):
                return True
        for c in ancestor.else_children:
            if c.step_id == target.step_id:
                return True
            if self._is_descendant_of(c, target):
                return True
        return False

    # ── Toolbar actions ─────────────────────────────────────────────────

    def _save_flow(self) -> bool:
        if self._flow_is_saved:
            name = self._flow.name
        else:
            name = ResourcePathDialog.get_path(
                title="Save Flow",
                item_label="Flow",
                current_path=self._flow.name if self._flow.name != "Untitled" else "",
                ignore_path="",
                allow_existing=self._allow_unsaved_overwrite,
                known_paths=self._db.list_resource_paths(),
                known_folders=self._db.list_resource_folders(),
                existing_paths=self._db.list_flows(),
                parent=self,
            )
            if name is None:
                return False
            collision = find_resource_collision(name, self._db.list_flows())
            if collision is not None:
                reply = QMessageBox.question(
                    self,
                    "Replace Flow?",
                    f"A flow named '{collision}' already exists. Replace it?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return False
                name = collision
        report = preflight_flow(
            self._flow,
            db=self._db,
            root_name=name,
            mode=PreflightMode.DOCUMENT,
        )
        if not confirm_document_preflight(self, report, title="Save Flow Preflight"):
            return False

        self._flow.name = name
        self._db.save_flow(name, self._flow)
        self._flow_is_saved = True
        self._allow_unsaved_overwrite = False
        self._mark_clean()
        self._update_saved_flow_actions()
        window = self.window()
        if hasattr(window, "statusBar"):
            window.statusBar().showMessage(f"Flow '{name}' saved", 5000)
        return True

    def _load_flow(self) -> None:
        if not self.confirm_discard_changes():
            return
        flows = sorted(self._db.list_flows(), key=str.casefold)
        if not flows:
            QMessageBox.information(self, "Load Flow", "No saved flows found.")
            return
        name = self._choose_flow_to_load(flows)
        if not name:
            return
        flow = self._db.load_flow(name)
        if flow:
            self._flow = flow
            self._reset_command_section_states()
            self._rebuild_cards()
            self._flow_is_saved = True
            self._allow_unsaved_overwrite = False
            self._mark_clean()
            self._update_saved_flow_actions()

    def _choose_flow_to_load(self, flows: list[str]) -> str | None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Load Flow")
        dlg.resize(520, 560)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(QLabel("Select flow:"))

        search_edit = QLineEdit()
        search_edit.setPlaceholderText("Search flows...")
        layout.addWidget(search_edit)

        tree_widget = ResourceTreeWidget()
        tree_widget.resource_activated.connect(lambda _name: dlg.accept())
        layout.addWidget(tree_widget, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Open
            | QDialogButtonBox.StandardButton.Cancel
        )
        open_btn = buttons.button(QDialogButtonBox.StandardButton.Open)
        if open_btn is not None:
            open_btn.setText("Load")
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        def refresh_list() -> None:
            query = search_edit.text().strip().casefold()
            tree_widget.set_resources(
                [(flow_name, flow_name) for flow_name in flows],
                query=query,
                canonical_folders=self._db.list_resource_folders(),
            )
            if open_btn is not None:
                open_btn.setEnabled(tree_widget.selected_resource_id() is not None)

        search_edit.textChanged.connect(refresh_list)
        search_edit.returnPressed.connect(
            lambda: dlg.accept() if tree_widget.selected_resource_id() is not None else None
        )
        tree_widget.itemSelectionChanged.connect(
            lambda: open_btn.setEnabled(tree_widget.selected_resource_id() is not None)
            if open_btn is not None
            else None
        )
        refresh_list()
        search_edit.setFocus()

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return tree_widget.selected_resource_id()

    def _import_flow(self) -> None:
        if not self.confirm_discard_changes():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Flow", "", "JSON Files (*.json);;All Files (*)"
        )
        if path:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                imported_flow = AutomationFlow.from_dict(data)
                report = preflight_flow(
                    imported_flow,
                    db=self._db,
                    root_name=imported_flow.name,
                    mode=PreflightMode.DOCUMENT,
                )
                if not confirm_import_preflight(self, report):
                    return
                self._flow = imported_flow
                self._reset_command_section_states()
                self._flow_is_saved = False
                self._allow_unsaved_overwrite = True
                self._rebuild_cards()
                self._update_saved_flow_actions()
            except Exception as e:
                QMessageBox.critical(self, "Import Error", str(e))

    def _export_flow(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Flow",
            f"{safe_export_stem(self._flow.name)}.json",
            "JSON Files (*.json);;All Files (*)",
        )
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self._flow.to_dict(), f, indent=2)
                QMessageBox.information(
                    self, "Exported", f"Flow exported to {path}"
                )
            except Exception as e:
                QMessageBox.critical(self, "Export Error", str(e))

    def _clear_flow(self) -> None:
        if not self._flow.commands:
            return
        reply = QMessageBox.question(
            self,
            "Clear Flow",
            "Remove all commands and make this an unsaved blank flow?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._flow.name = "Untitled"
            self._flow.commands.clear()
            self._reset_command_section_states()
            self._rebuild_cards()
            self._flow_is_saved = False
            self._allow_unsaved_overwrite = False
            self._update_saved_flow_actions()

    def _new_flow(self) -> None:
        if not self.confirm_discard_changes():
            return
        self._flow = AutomationFlow(
            name="Untitled",
            value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        )
        self._reset_command_section_states()
        self._flow_is_saved = False
        self._allow_unsaved_overwrite = False
        self._rebuild_cards()
        self._mark_clean()
        self._update_saved_flow_actions()

    # ── Rename / Delete (saved flows only) ──────────────────────────

    def _update_saved_flow_actions(self) -> None:
        self._rename_flow_btn.setEnabled(self._flow_is_saved)
        self._delete_flow_btn.setEnabled(self._flow_is_saved)
        self._versions_flow_btn.setEnabled(self._flow_is_saved)

    def _rename_flow(self) -> None:
        if not self._flow_is_saved:
            return
        old_name = self._flow.name
        new_name = ResourcePathDialog.get_path(
            title="Move or Rename Flow",
            item_label="Flow",
            current_path=old_name,
            known_paths=self._db.list_resource_paths(),
            known_folders=self._db.list_resource_folders(),
            existing_paths=self._db.list_flows(),
            parent=self,
        )
        if new_name is None or new_name == old_name:
            return
        existing = self._db.list_flows()
        if new_name in existing:
            QMessageBox.warning(self, "Name Taken", f"A flow named '{new_name}' already exists.")
            return
        self._db.rename_flow(old_name, new_name)
        self.on_resource_renamed("flow", old_name, new_name)
        self.resource_renamed.emit("flow", old_name, new_name)

    def _delete_flow(self) -> None:
        if not self._flow_is_saved:
            return
        references = self._db.flow_references(self._flow.name)
        details: list[str] = []
        if references["flows"]:
            details.append("Sub-flow callers: " + ", ".join(references["flows"]))
        if references["run_configs"]:
            details.append("Saved run setups: " + ", ".join(references["run_configs"]))
        impact = ""
        if details:
            impact = (
                "\n\nDeleting it will leave broken links in:\n"
                + "\n".join(details)
            )
        reply = QMessageBox.question(
            self, "Delete Flow",
            f"Delete flow '{self._flow.name}'? This cannot be undone.{impact}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        deleted_name = self._flow.name
        self._db.delete_flow(deleted_name, force=True)
        self._flow = AutomationFlow(
            name="Untitled",
            value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        )
        self._reset_command_section_states()
        self._flow_is_saved = False
        self._allow_unsaved_overwrite = False
        self._rebuild_cards()
        self._update_saved_flow_actions()
        self.flow_deleted.emit(deleted_name)
        self.resource_deleted.emit("flow", deleted_name)

    def _show_flow_versions(self) -> None:
        if not self._flow_is_saved:
            return
        versions = self._db.list_flow_versions(self._flow.name)
        if not versions:
            QMessageBox.information(
                self,
                "Flow Versions",
                f"No previous versions recorded for '{self._flow.name}' yet.",
            )
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Versions: {self._flow.name}")
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("Select an older version to load into the editor:"))

        list_widget = QListWidget()
        for version in versions:
            item = QListWidgetItem(
                f"{version['saved_at']}  ({version['change_type']})"
            )
            item.setData(Qt.ItemDataRole.UserRole, version["id"])
            list_widget.addItem(item)
        list_widget.setCurrentRow(0)
        layout.addWidget(list_widget)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Open
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        item = list_widget.currentItem()
        if item is None:
            return
        restored = self._db.load_flow_version(item.data(Qt.ItemDataRole.UserRole))
        if restored is None:
            QMessageBox.warning(self, "Flow Versions", "Could not load that version.")
            return
        restored.name = self._flow.name
        self._flow = restored
        self._flow_is_saved = True
        self._allow_unsaved_overwrite = False
        self._rebuild_cards()
        self._update_saved_flow_actions()
        QMessageBox.information(
            self,
            "Version Loaded",
            "The selected version is now loaded as unsaved changes. "
            "Click Save to restore it over the current flow.",
        )

    def on_resource_renamed(self, kind: str, old_name: str, new_name: str) -> None:
        """Keep the open document and its clean baseline aligned with DB renames."""
        self.on_resources_renamed({kind: [(old_name, new_name)]})

    def on_resources_renamed(
        self,
        changes: dict[str, list[tuple[str, str]]],
    ) -> None:
        """Apply a batch of library renames with one editor rebuild."""
        keys_by_kind = {
            "flow": (("flow_name",), ()),
            "image": (("image_name",), ("images",)),
            "visual_rule": (("target_rule", "bonus_rule"), ()),
        }
        clean_flow = AutomationFlow.from_dict(json.loads(self._clean_snapshot))
        changed = False
        for kind, renames in changes.items():
            keys = keys_by_kind.get(kind)
            if keys is None:
                continue
            scalar_keys, list_keys = keys
            for old_name, new_name in renames:
                changed = True
                if kind == "flow" and self._flow.name == old_name:
                    self._flow.name = new_name
                if kind == "flow" and clean_flow.name == old_name:
                    clean_flow.name = new_name
                self._replace_live_references(
                    self._flow.commands, old_name, new_name, scalar_keys, list_keys
                )
                self._replace_live_hook_references(
                    self._flow, old_name, new_name, scalar_keys, list_keys
                )
                self._replace_live_references(
                    clean_flow.commands, old_name, new_name, scalar_keys, list_keys
                )
                self._replace_live_hook_references(
                    clean_flow, old_name, new_name, scalar_keys, list_keys
                )
        if not changed:
            return
        self._clean_snapshot = json.dumps(
            clean_flow.to_dict(), sort_keys=True, separators=(",", ":")
        )
        self._rebuild_cards()
        self._refresh_document_state()

    def on_flow_renamed(self, old_name: str, new_name: str) -> None:
        """Backward-compatible slot for flow-only rename notifications."""
        self.on_resource_renamed("flow", old_name, new_name)

    def on_flow_deleted(self, name: str) -> None:
        """Backward-compatible slot for flow-only deletion notifications."""
        self.on_resource_deleted("flow", name)

    def on_resource_deleted(self, kind: str, name: str) -> None:
        """Refresh selectors and clear the editor if its saved flow was deleted."""
        if kind == "flow" and self._flow_is_saved and self._flow.name == name:
            self._flow = AutomationFlow(
                name="Untitled",
                value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
            )
            self._reset_command_section_states()
            self._flow_is_saved = False
            self._allow_unsaved_overwrite = False
            self._rebuild_cards()
            self._update_saved_flow_actions()
            return
        if kind in {"flow", "image", "visual_rule"}:
            self._rebuild_cards()

    # ── Run single command ───────────────────────────────────────────

    def _get_executor(self):
        """Lazy-build a FlowExecutor (same handler set as runner_view)."""
        if self._executor is not None:
            return self._executor
        from automadin.ui.flow_runner.runner_view import _build_executor
        self._executor = _build_executor(db=self._db)
        return self._executor

    def run_single_command(self, cmd: FlowCommand) -> None:
        """Execute a single command using the currently connected device session.

        Every target runs outside the GUI thread. Browser commands are submitted
        to the selected session's Playwright owner; Android and Windows commands
        are serialized through the selected session's operation lease.
        """
        controller = self._run_step_controller
        if controller.is_busy:
            if controller.active_step_id == cmd.step_id:
                controller.cancel(controller.active_operation_id)
                return
            active_label = self._run_step_label(controller.active_step_id or "")
            QMessageBox.information(
                self,
                "Run Step In Progress",
                f"'{active_label}' is still running. Stop it or wait for it to "
                "finish before starting another step.",
            )
            return

        if not cmd.enabled:
            QMessageBox.information(
                self,
                "Command Disabled",
                "Enable this command before running it directly.",
            )
            return

        session = self._device_selector.get_selected_session()
        if session is None:
            QMessageBox.warning(
                self,
                "No Device",
                "Connect to a device first using the device selector above.",
            )
            return

        step_flow = AutomationFlow(
            name=self._flow.name,
            commands=[cmd],
            parameters=list(self._flow.parameters),
            flow_id=self._flow.flow_id,
            value_model_version=self._flow.value_model_version,
            inputs=list(self._flow.inputs),
            variables=list(self._flow.variables),
            outputs=list(self._flow.outputs),
            resolution_scaling=self._flow.resolution_scaling,
            hooks=list(self._flow.hooks),
        )
        report = preflight_flow(
            step_flow,
            db=self._db,
            root_name=self._flow.name,
            target=target_kind_for_device(session.device),
            mode=PreflightMode.EXECUTION,
        )
        if not confirm_execution_preflight(self, report, title="Step Cannot Run"):
            return

        final_report = preflight_flow(
            step_flow,
            db=self._db,
            root_name=self._flow.name,
            target=target_kind_for_device(session.device),
            mode=PreflightMode.EXECUTION,
        )
        if final_report.errors:
            details = "\n".join(
                f"• {issue.message}" for issue in final_report.errors[:8]
            )
            QMessageBox.critical(
                self,
                "Step Changed Before Run",
                "The step changed after validation and is no longer safe to run."
                f"\n\n{details}",
            )
            return

        executor = self._get_executor()
        try:
            context = build_debug_context(self._flow, session)
            context.interactive_resolution_scaling = self._flow.resolution_scaling
            context.interactive_flow = step_flow
        except FlowRuntimeValueError as exc:
            QMessageBox.critical(self, "Step Cannot Run", str(exc))
            return
        try:
            controller.start(
                executor,
                cmd,
                session,
                self._flow.name,
                context=context,
            )
        except RunStepBusyError as exc:
            QMessageBox.information(self, "Run Step In Progress", str(exc))
        except RunStepStartError as exc:
            if exc.cleanup_pending:
                label = self._run_step_label(
                    controller.active_step_id or cmd.step_id
                )
                if controller.state == "cleanup_pending":
                    self._set_run_step_status(f"Cleanup pending: {label}")
                    QMessageBox.warning(
                        self,
                        "Run Step Cleanup Pending",
                        "The step could not start cleanly and still owns device "
                        "cleanup. Use Retry cleanup on the active step; it "
                        "remains unavailable until cleanup succeeds.",
                    )
                else:
                    self._set_run_step_status(f"Stopping: {label}…")
                    QMessageBox.warning(
                        self,
                        "Run Step Is Stopping",
                        "The step began launching, but Automadin could not "
                        "confirm a clean startup. A stop was requested and the "
                        "step remains unavailable until shutdown finishes.",
                    )
            else:
                self._set_run_step_status("Run Step could not start")
                QMessageBox.critical(
                    self,
                    "Run Step Could Not Start",
                    str(exc),
                )

    def _test_hook_condition(self, hook: AutomationHook) -> None:
        """Probe an unsaved condition through normal target ownership paths."""
        controller = self._run_step_controller
        if controller.is_busy:
            QMessageBox.information(
                self,
                "Automation Operation In Progress",
                "Wait for the active Run Step or hook test to finish first.",
            )
            return
        session = self._device_selector.get_selected_session()
        if session is None:
            QMessageBox.warning(
                self,
                "No Device",
                "Connect to the target device or browser session before testing.",
            )
            return
        command = FlowCommand("check_hooks", params={"hooks": [hook.hook_id]})
        probe_flow = AutomationFlow(
            name=self._flow.name,
            commands=[command],
            parameters=list(self._flow.parameters),
            flow_id=self._flow.flow_id,
            value_model_version=self._flow.value_model_version,
            inputs=list(self._flow.inputs),
            variables=list(self._flow.variables),
            outputs=list(self._flow.outputs),
            resolution_scaling=self._flow.resolution_scaling,
            hooks=[hook],
        )
        report = preflight_flow(
            probe_flow,
            db=self._db,
            root_name=self._flow.name,
            target=target_kind_for_device(session.device),
            mode=PreflightMode.EXECUTION,
        )
        if report.errors:
            details = "\n".join(
                f"• {issue.message}" for issue in report.errors[:8]
            )
            QMessageBox.warning(
                self,
                "Hook Condition Cannot Be Tested",
                details,
            )
            return
        try:
            context = build_debug_context(probe_flow, session)
            context.interactive_resolution_scaling = probe_flow.resolution_scaling
            context.interactive_flow = probe_flow
            context.hook_probe_only = True
            operation_id = controller.start(
                self._get_executor(),
                command,
                session,
                probe_flow.name,
                context=context,
            )
        except (RunStepBusyError, RunStepStartError, FlowRuntimeValueError) as exc:
            QMessageBox.warning(self, "Hook Test Could Not Start", str(exc))
            return
        self._hook_probe_operations[operation_id] = (hook.hook_id, hook.name)
        self._set_run_step_status(f"Testing hook condition: {hook.name}…")

    def _on_hook_probe_completed(
        self,
        operation_id: str,
        _step_id: str,
        results: object,
    ) -> None:
        probe = self._hook_probe_operations.pop(operation_id, None)
        if probe is None or not isinstance(results, dict):
            return
        hook_id, hook_name = probe
        raw_result = results.get(hook_id, False)
        detail = raw_result if isinstance(raw_result, dict) else {}
        matched = bool(detail.get("matched", False)) if detail else bool(raw_result)
        verdict = "matched" if matched else "did not match"
        self._set_run_step_status(f"Hook condition {verdict}: {hook_name}")
        evidence = ""
        if detail.get("match_x") is not None and detail.get("match_y") is not None:
            image_name = str(detail.get("matched_image") or "image")
            confidence = detail.get("match_confidence")
            confidence_text = (
                f" at {float(confidence):.4f} confidence"
                if confidence is not None
                else ""
            )
            evidence = (
                f"\n\nMatched '{image_name}' at "
                f"({detail['match_x']}, {detail['match_y']})"
                f"{confidence_text}."
            )
        QMessageBox.information(
            self,
            "Hook Condition Test",
            f"'{hook_name}' {verdict} the current target state.\n\n"
            f"No hook actions were run and no hook latch was changed.{evidence}",
        )

    def _run_step_label(self, step_id: str) -> str:
        command = self._find_by_step_id(self._flow.commands, step_id)
        if command is None:
            return "Run Step"
        definition = self._command_types.get(command.id, {})
        return str(definition.get("label") or command.id)

    def _set_run_step_status(self, message: str) -> None:
        self._run_step_status_label.setText(message)
        self._run_step_status_label.setVisible(bool(message))

    def _on_run_step_state_changed(
        self,
        _operation_id: str,
        step_id: str,
        state: str,
    ) -> None:
        self.run_step_state_changed.emit(step_id, state)
        self._set_run_step_flow_controls_locked(state != "idle")
        label = self._run_step_label(step_id)
        if state == "running":
            self._set_run_step_status(f"Running: {label}")
        elif state == "cancel_requested":
            self._set_run_step_status(f"Stopping: {label}…")
        elif state == "cleanup_pending":
            self._set_run_step_status(f"Cleanup pending: {label}")
        elif state == "idle":
            self._set_run_step_status("")

    def _set_run_step_flow_controls_locked(self, locked: bool) -> None:
        """Keep the active Run Step card present until its worker becomes idle."""
        if locked == self._run_step_flow_controls_locked:
            return
        if locked:
            self._run_step_flow_control_states = [
                (control, control.isEnabled())
                for control in self._run_step_flow_mutation_controls
            ]
            for control, _was_enabled in self._run_step_flow_control_states:
                control.setEnabled(False)
        else:
            for control, was_enabled in self._run_step_flow_control_states:
                control.setEnabled(was_enabled)
            self._run_step_flow_control_states = []
        self._run_step_flow_controls_locked = locked

    def _on_run_step_succeeded(
        self,
        _operation_id: str,
        step_id: str,
    ) -> None:
        self._set_run_step_status(f"Completed: {self._run_step_label(step_id)}")

    def _on_run_step_failed(
        self,
        operation_id: str,
        step_id: str,
        safe_message: str,
    ) -> None:
        probe = self._hook_probe_operations.pop(operation_id, None)
        label = self._run_step_label(step_id)
        self._set_run_step_status(f"Failed: {label}")
        QMessageBox.critical(
            self,
            "Hook Condition Test Failed" if probe is not None else "Run Step Failed",
            safe_message,
        )

    def _on_run_step_cancelled(
        self,
        operation_id: str,
        step_id: str,
    ) -> None:
        self._hook_probe_operations.pop(operation_id, None)
        self._set_run_step_status(f"Stopped: {self._run_step_label(step_id)}")

    def shutdown(self, *, wait_ms: int = 0) -> bool:
        """Request Run Step cancellation and retain any still-live worker."""
        return self._run_step_controller.shutdown(wait_ms)
