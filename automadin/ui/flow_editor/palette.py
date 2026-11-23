"""Searchable command library used to add commands to a flow."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
    QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from automadin.ui.flow_editor.command_discovery import (
    COMMAND_DISCOVERY,
    PURPOSES,
    TARGETS,
    target_explanation,
    target_summary,
    validate_command_discovery,
)
from automadin.ui.theme import COLORS


_COMMAND_ROLE = Qt.ItemDataRole.UserRole
_PURPOSE_ROLE = Qt.ItemDataRole.UserRole


class CommandPaletteDialog(QDialog):
    """Browse commands by purpose and target, with details before insertion."""

    def __init__(
        self,
        command_types: dict[str, dict[str, Any]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        validate_command_discovery(command_types, require_complete=False)
        self.setWindowTitle("Add Command")
        self.resize(1160, 680)
        self.setMinimumSize(900, 540)
        self.setStyleSheet(self._dialog_stylesheet())

        self.selected_command_id: str | None = None
        self._command_types = command_types
        self._active_target = "all"
        self._active_purpose = "all"
        self._tiles: list[tuple[QTreeWidgetItem, dict[str, Any]]] = []

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(14, 14, 14, 14)
        main_layout.setSpacing(10)

        self._search = QLineEdit()
        self._search.setObjectName("commandSearch")
        self._search.setPlaceholderText(
            "Search commands — try “click image”, “open website”, or “repeat”"
        )
        self._search.setClearButtonEnabled(True)
        self._search.setAccessibleName("Search commands")
        self._search.textChanged.connect(self._refresh_results)
        self._search.installEventFilter(self)
        main_layout.addWidget(self._search)

        target_bar = QHBoxLayout()
        target_bar.setSpacing(6)
        target_label = QLabel("Works with")
        target_label.setObjectName("filterLabel")
        target_bar.addWidget(target_label)
        self._target_group = QButtonGroup(self)
        self._target_group.setExclusive(True)
        self._target_buttons: dict[str, QPushButton] = {}
        target_options = (("all", "All"),) + tuple(
            (item.key, item.label) for item in TARGETS
        )
        for key, label in target_options:
            button = QPushButton(label)
            button.setObjectName("commandFilterButton")
            button.setCheckable(True)
            button.setMinimumHeight(32)
            button.setAccessibleName(f"Show {label} commands")
            button.clicked.connect(
                lambda checked=False, target=key: self._set_target(target)
            )
            self._target_group.addButton(button)
            self._target_buttons[key] = button
            target_bar.addWidget(button)
        self._target_buttons["all"].setChecked(True)
        target_bar.addStretch()
        main_layout.addLayout(target_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setObjectName("commandLibrarySplitter")

        purpose_panel = QWidget()
        purpose_layout = QVBoxLayout(purpose_panel)
        purpose_layout.setContentsMargins(0, 0, 0, 0)
        purpose_layout.setSpacing(5)
        purpose_heading = QLabel("BY PURPOSE")
        purpose_heading.setObjectName("panelHeading")
        purpose_layout.addWidget(purpose_heading)
        self._purposes = QListWidget()
        self._purposes.setObjectName("purposeList")
        purpose_panel.setMaximumWidth(190)
        self._purposes.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._purposes.setAccessibleName("Command purposes")
        all_item = QListWidgetItem("All commands")
        all_item.setData(_PURPOSE_ROLE, "all")
        self._purposes.addItem(all_item)
        for purpose in PURPOSES:
            item = QListWidgetItem(purpose.label)
            item.setData(_PURPOSE_ROLE, purpose.key)
            item.setToolTip(purpose.description)
            self._purposes.addItem(item)
        self._purposes.setCurrentRow(0)
        self._purposes.currentItemChanged.connect(self._purpose_changed)
        purpose_layout.addWidget(self._purposes, 1)
        splitter.addWidget(purpose_panel)

        results_panel = QWidget()
        results_layout = QVBoxLayout(results_panel)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_layout.setSpacing(5)
        results_top = QHBoxLayout()
        results_heading = QLabel("COMMANDS")
        results_heading.setObjectName("panelHeading")
        results_top.addWidget(results_heading)
        results_top.addStretch()
        self._result_count = QLabel()
        self._result_count.setObjectName("resultCount")
        results_top.addWidget(self._result_count)
        results_layout.addLayout(results_top)

        self._results = QTreeWidget()
        self._results.setObjectName("commandResults")
        self._results.setColumnCount(3)
        self._results.setHeaderLabels(("Command", "What it does", "Works with"))
        self._results.setRootIsDecorated(False)
        self._results.setIndentation(0)
        self._results.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._results.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._results.setAccessibleName("Available commands")
        header = self._results.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setMinimumSectionSize(120)
        self._results.currentItemChanged.connect(self._selection_changed)
        self._results.itemActivated.connect(self._activate_item)
        results_layout.addWidget(self._results, 1)
        self._empty_label = QLabel("No commands match these filters.")
        self._empty_label.setObjectName("emptyResults")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.hide()
        results_layout.addWidget(self._empty_label)
        splitter.addWidget(results_panel)

        details = QFrame()
        details.setObjectName("commandDetails")
        details.setMinimumWidth(245)
        details.setMaximumWidth(320)
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(16, 14, 16, 14)
        details_layout.setSpacing(8)
        details_heading = QLabel("COMMAND DETAILS")
        details_heading.setObjectName("panelHeading")
        details_layout.addWidget(details_heading)
        self._detail_name = QLabel("Select a command")
        self._detail_name.setObjectName("detailName")
        self._detail_name.setWordWrap(True)
        details_layout.addWidget(self._detail_name)
        self._detail_target = QLabel("")
        self._detail_target.setObjectName("targetBadge")
        self._detail_target.setWordWrap(True)
        details_layout.addWidget(self._detail_target)
        self._detail_description = QLabel(
            "Choose a command to see where it works and what it does."
        )
        self._detail_description.setObjectName("detailDescription")
        self._detail_description.setWordWrap(True)
        details_layout.addWidget(self._detail_description)
        self._detail_applicability = QLabel("")
        self._detail_applicability.setObjectName("detailApplicability")
        self._detail_applicability.setWordWrap(True)
        details_layout.addWidget(self._detail_applicability)
        details_layout.addStretch()
        self._detail_id = QLabel("")
        self._detail_id.setObjectName("technicalCommandId")
        self._detail_id.setWordWrap(True)
        details_layout.addWidget(self._detail_id)
        hint = QLabel("Parameters are configured after adding the command.")
        hint.setObjectName("detailHint")
        hint.setWordWrap(True)
        details_layout.addWidget(hint)
        action_row = QHBoxLayout()
        self._cancel_button = QPushButton("Cancel")
        self._cancel_button.clicked.connect(self.reject)
        action_row.addWidget(self._cancel_button)
        self._add_button = QPushButton("Add command")
        self._add_button.setProperty("cssClass", "primary")
        self._add_button.setDefault(True)
        self._add_button.setEnabled(False)
        self._add_button.clicked.connect(self._accept_current)
        action_row.addWidget(self._add_button)
        details_layout.addLayout(action_row)
        splitter.addWidget(details)

        splitter.setSizes((175, 680, 275))
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        main_layout.addWidget(splitter, 1)

        self._refresh_results()
        self._search.setFocus()

    def _dialog_stylesheet(self) -> str:
        return f"""
            QDialog {{ background-color: {COLORS['bg_darkest']}; }}
            QLabel#filterLabel, QLabel#resultCount, QLabel#detailHint,
            QLabel#technicalCommandId {{ color: {COLORS['text_tertiary']}; }}
            QLabel#panelHeading {{
                color: {COLORS['text_secondary']}; font-size: 10pt;
                font-weight: bold; padding: 2px 0;
            }}
            QPushButton#commandFilterButton {{
                background-color: transparent; color: {COLORS['text_secondary']};
                border: 1px solid {COLORS['border']}; border-radius: 6px;
                padding: 4px 12px;
            }}
            QPushButton#commandFilterButton:hover {{
                background-color: {COLORS['bg_hover']}; color: {COLORS['text_primary']};
            }}
            QPushButton#commandFilterButton:checked {{
                background-color: {COLORS['primary']}; border-color: {COLORS['primary']};
                color: {COLORS['primary_text']};
            }}
            QListWidget#purposeList, QTreeWidget#commandResults {{
                background-color: {COLORS['bg_surface']}; color: {COLORS['text_primary']};
                border: 1px solid {COLORS['border_light']}; border-radius: 7px;
                outline: none;
            }}
            QListWidget#purposeList::item {{ padding: 8px 9px; border-radius: 4px; }}
            QListWidget#purposeList::item:hover {{ background: {COLORS['bg_hover']}; }}
            QListWidget#purposeList::item:selected {{
                background: {COLORS['bg_elevated']}; color: {COLORS['text_primary']};
                border-left: 3px solid {COLORS['primary']};
            }}
            QTreeWidget#commandResults::item {{
                min-height: 38px; padding: 4px 6px;
                border-bottom: 1px solid {COLORS['border_light']};
            }}
            QTreeWidget#commandResults::item:hover {{ background: {COLORS['bg_hover']}; }}
            QTreeWidget#commandResults::item:selected {{
                background: {COLORS['primary']}; color: {COLORS['primary_text']};
            }}
            QHeaderView::section {{
                background: {COLORS['bg_elevated']}; color: {COLORS['text_secondary']};
                border: none; border-bottom: 1px solid {COLORS['border']};
                padding: 7px; font-weight: bold;
            }}
            QFrame#commandDetails {{
                background-color: {COLORS['bg_surface']};
                border: 1px solid {COLORS['border_light']}; border-radius: 7px;
            }}
            QLabel#detailName {{
                color: {COLORS['text_primary']}; font-size: 14pt; font-weight: bold;
            }}
            QLabel#targetBadge {{
                color: {COLORS['accent']}; font-size: 10pt; font-weight: bold;
            }}
            QLabel#detailDescription {{ color: {COLORS['text_primary']}; }}
            QLabel#detailApplicability {{ color: {COLORS['text_secondary']}; }}
            QLabel#emptyResults {{ color: {COLORS['text_secondary']}; padding: 20px; }}
        """

    def _set_target(self, target: str) -> None:
        self._active_target = target
        self._refresh_results()

    def _purpose_changed(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        self._active_purpose = (
            str(current.data(_PURPOSE_ROLE)) if current is not None else "all"
        )
        self._refresh_results()

    def _matches_search(self, command_id: str, definition: dict[str, Any]) -> bool:
        search = self._search.text().strip().casefold()
        if not search:
            return True
        discovery = COMMAND_DISCOVERY[command_id]
        target_labels = " ".join(
            target.label for target in TARGETS if target.key in discovery.targets
        )
        purpose_label = next(
            purpose.label for purpose in PURPOSES if purpose.key == discovery.purpose
        )
        searchable = " ".join(
            (
                command_id,
                str(definition.get("label", "")),
                str(definition.get("description", "")),
                target_labels,
                purpose_label,
                *discovery.keywords,
            )
        ).casefold()
        return all(term in searchable for term in search.split())

    def _visible_definitions(self) -> list[tuple[str, dict[str, Any]]]:
        visible: list[tuple[str, dict[str, Any]]] = []
        for command_id, definition in self._command_types.items():
            if definition.get("paletteHidden", False):
                continue
            discovery = COMMAND_DISCOVERY[command_id]
            if self._active_target != "all":
                is_general = discovery.targets == ("general",)
                if self._active_target == "general":
                    if not is_general:
                        continue
                elif self._active_target not in discovery.targets and not is_general:
                    continue
            if self._active_purpose != "all" and discovery.purpose != self._active_purpose:
                continue
            if self._matches_search(command_id, definition):
                visible.append((command_id, definition))
        return visible

    def _refresh_results(self) -> None:
        previous_id = self._current_command_id()
        self._results.blockSignals(True)
        self._results.clear()
        self._tiles.clear()
        visible = self._visible_definitions()
        grouped: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for command_id, definition in visible:
            purpose = COMMAND_DISCOVERY[command_id].purpose
            grouped.setdefault(purpose, []).append((command_id, definition))

        selected_item: QTreeWidgetItem | None = None
        first_command: QTreeWidgetItem | None = None
        for purpose in PURPOSES:
            commands = grouped.get(purpose.key, [])
            if not commands:
                continue
            group_item = QTreeWidgetItem((purpose.label, "", ""))
            group_item.setFirstColumnSpanned(True)
            group_item.setFlags(group_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            font = group_item.font(0)
            font.setBold(True)
            group_item.setFont(0, font)
            group_item.setForeground(0, QColor(COLORS["text_secondary"]))
            self._results.addTopLevelItem(group_item)
            for command_id, definition in sorted(
                commands,
                key=lambda pair: str(pair[1].get("label", pair[0])).casefold(),
            ):
                discovery = COMMAND_DISCOVERY[command_id]
                item = QTreeWidgetItem(
                    (
                        str(definition.get("label", command_id)),
                        str(definition.get("description", "")),
                        target_summary(discovery.targets),
                    )
                )
                item.setData(0, _COMMAND_ROLE, command_id)
                item.setToolTip(0, command_id)
                group_item.addChild(item)
                self._tiles.append((item, definition))
                first_command = first_command or item
                if command_id == previous_id:
                    selected_item = item
            group_item.setExpanded(True)

        self._results.blockSignals(False)
        count = len(visible)
        self._result_count.setText(f"{count} command" + ("" if count == 1 else "s"))
        self._empty_label.setVisible(count == 0)
        self._results.setVisible(count > 0)
        item_to_select = selected_item or first_command
        if item_to_select is not None:
            self._results.setCurrentItem(item_to_select)
            self._selection_changed(item_to_select, None)
        else:
            self._clear_details()

    def _current_command_id(self) -> str | None:
        item = self._results.currentItem()
        if item is None:
            return None
        command_id = item.data(0, _COMMAND_ROLE)
        return str(command_id) if command_id else None

    def _selection_changed(
        self,
        current: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        command_id = current.data(0, _COMMAND_ROLE) if current is not None else None
        if not command_id:
            self._clear_details()
            return
        command_id = str(command_id)
        definition = self._command_types[command_id]
        discovery = COMMAND_DISCOVERY[command_id]
        self._detail_name.setText(str(definition.get("label", command_id)))
        self._detail_target.setText(target_summary(discovery.targets))
        self._detail_description.setText(str(definition.get("description", "")))
        self._detail_applicability.setText(target_explanation(discovery.targets))
        self._detail_id.setText(f"Technical name: {command_id}")
        self._add_button.setEnabled(True)

    def _clear_details(self) -> None:
        self._detail_name.setText("No command selected")
        self._detail_target.clear()
        self._detail_description.setText("Adjust the search or filters to find a command.")
        self._detail_applicability.clear()
        self._detail_id.clear()
        self._add_button.setEnabled(False)

    def _activate_item(self, item: QTreeWidgetItem, _column: int) -> None:
        if item.data(0, _COMMAND_ROLE):
            self._accept_current()

    def _accept_current(self) -> None:
        command_id = self._current_command_id()
        if command_id is None:
            return
        self.selected_command_id = command_id
        self.accept()

    def eventFilter(self, watched: Any, event: QEvent) -> bool:
        if (
            watched is self._search
            and event.type() == QEvent.Type.KeyPress
            and isinstance(event, QKeyEvent)
            and event.key() == Qt.Key.Key_Down
        ):
            if self._results.currentItem() is not None:
                self._results.setFocus()
                return True
        return super().eventFilter(watched, event)

    def get_selected(self) -> str | None:
        """Return the selected command ID, or ``None`` when cancelled."""
        return self.selected_command_id
