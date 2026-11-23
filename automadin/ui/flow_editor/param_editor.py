"""Parameter editor widgets for flow command configuration."""

from __future__ import annotations

from typing import Any, Callable
from collections.abc import Mapping

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from automadin.models.resource_path import resource_display_label, resource_sort_key
from automadin.models.locator import LocatorStrategy, normalize_locator
from automadin.models.flow_value import FlowValueCatalog
from automadin.models.typed_value import (
    ValueCoercionError,
    ValueType,
    canonical_typed_value_text,
    coerce_typed_value,
    is_value_type_assignable,
)
from automadin.models.value_reference import (
    LiteralValueReference,
    ValueReferenceError,
    value_reference_from_dict,
)
from automadin.ui.flow_editor.value_template_editor import ValueTemplateEditorWidget
from automadin.ui.theme import COLORS

DEFAULT_INT_PARAM_MAX = 999999
TIMING_INT_PARAM_MAX = 9999999
IMAGE_NAME_ROLE = int(Qt.ItemDataRole.UserRole)
IMAGE_ORDER_ROLE = IMAGE_NAME_ROLE + 1


def _int_param_max(param_def: dict[str, Any]) -> int:
    """Return a max value for integer fields, widening time-related params."""
    if "max" in param_def:
        return int(param_def["max"])

    name = str(param_def.get("name", "")).lower()
    label = str(param_def.get("label", "")).lower()
    normalized_name = name.replace("_", "")
    is_timing_param = (
        "timeout" in normalized_name
        or name in {"ms", "min_ms", "max_ms"}
        or name.endswith("_ms")
        or "timeout" in label
        or "duration (ms)" in label
        or "wait" in label
        or "interval" in label
    )
    if is_timing_param:
        return TIMING_INT_PARAM_MAX
    return DEFAULT_INT_PARAM_MAX


def _int_param_min(param_def: dict[str, Any]) -> int:
    """Return a min value for integer fields, allowing image-click offsets."""
    if "min" in param_def:
        return int(param_def["min"])

    name = str(param_def.get("name", "")).lower()
    label = str(param_def.get("label", "")).lower()
    if name in {"xmargin", "ymargin", "margin_x", "margin_y"}:
        return -DEFAULT_INT_PARAM_MAX
    if "offset" in label:
        return -DEFAULT_INT_PARAM_MAX
    return 0


class NoScrollSpinBox(QSpinBox):
    """QSpinBox that ignores wheel events unless it has focus (prevents
    accidental value changes while scrolling the flow editor)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event: Any) -> None:  # noqa: N802
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class NoScrollDoubleSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox that ignores wheel events unless it has focus."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event: Any) -> None:  # noqa: N802
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class NoScrollComboBox(QComboBox):
    """QComboBox that ignores wheel events unless it has focus."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event: Any) -> None:  # noqa: N802
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class PriorityImageItemDelegate(QStyledItemDelegate):
    """Paint selected image priorities as compact thumbnail tiles."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: Any) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = option.rect.adjusted(2, 2, -2, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        bg = COLORS["bg_elevated"] if selected else COLORS["bg_surface"]
        if hovered and not selected:
            bg = COLORS["bg_hover"]
        painter.setBrush(QColor(bg))
        border_color = COLORS["primary" if selected else "border_light"]
        painter.setPen(QPen(QColor(border_color), 1))
        painter.drawRoundedRect(rect, 5, 5)

        order = index.data(IMAGE_ORDER_ROLE) or index.row() + 1
        badge_text = str(order)
        badge_font = QFont(option.font)
        badge_font.setPointSizeF(10.0)
        badge_font.setBold(True)
        painter.setFont(badge_font)
        badge_metrics = QFontMetrics(badge_font)
        badge_width = max(18, badge_metrics.horizontalAdvance(badge_text) + 8)
        badge_rect = QRect(rect.left() + 5, rect.top() + 5, badge_width, 22)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(COLORS["primary"]))
        painter.drawRoundedRect(badge_rect, 4, 4)
        painter.setPen(QColor(COLORS["text_on_accent"]))
        painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge_text)

        icon = index.data(Qt.ItemDataRole.DecorationRole)
        icon_size = QSize(32, 32)
        icon_rect = QRect(
            rect.center().x() - icon_size.width() // 2,
            rect.top() + 7,
            icon_size.width(),
            icon_size.height(),
        )
        if isinstance(icon, QIcon) and not icon.isNull():
            icon.paint(painter, icon_rect)

        name = str(index.data(IMAGE_NAME_ROLE) or index.data() or "")
        text_rect = QRect(rect.left() + 5, rect.top() + 45, rect.width() - 10, 35)
        text_font = self._fit_text_font(option.font, name, text_rect)
        painter.setFont(text_font)
        painter.setPen(QColor(COLORS["text_primary"]))
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignHCenter
            | Qt.AlignmentFlag.AlignTop
            | Qt.TextFlag.TextWordWrap,
            self._two_line_text(name, text_rect, text_font),
        )
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: Any) -> QSize:
        return QSize(120, 86)

    @staticmethod
    def _two_line_text(text: str, rect: QRect, font: QFont) -> str:
        metrics = QFontMetrics(font)
        max_lines = max(1, min(2, rect.height() // max(1, metrics.lineSpacing())))
        words = text.replace("/", "/ ").split()
        if not words:
            return ""
        lines: list[str] = []
        while words and len(lines) < max_lines:
            line = words.pop(0)
            while (
                words
                and metrics.horizontalAdvance(f"{line} {words[0]}") <= rect.width()
            ):
                line = f"{line} {words.pop(0)}"
            if len(lines) == max_lines - 1 and words:
                line = metrics.elidedText(
                    f"{line} {' '.join(words)}",
                    Qt.TextElideMode.ElideRight,
                    rect.width(),
                )
                words.clear()
            lines.append(line.replace("/ ", "/"))
        return "\n".join(lines)

    @staticmethod
    def _fit_text_font(base_font: QFont, text: str, rect: QRect) -> QFont:
        font = QFont(base_font)
        # Asset names remain readable at desktop resolution. Long paths are
        # elided rather than shrinking below the application's 10pt floor.
        for size in (10.5, 10.0):
            font.setPointSizeF(size)
            metrics = QFontMetrics(font)
            if metrics.lineSpacing() * 2 <= rect.height():
                wrapped = PriorityImageItemDelegate._two_line_text(text, rect, font)
                if "..." not in wrapped or size == 10.0:
                    return font
        return font


class AutoFitImageListWidget(QListWidget):
    """Selected image list that fits fixed-size tiles into each row."""

    order_changed = Signal()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._tile_width = 132
        self._tile_height = 86
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setWrapping(True)
        self.setWordWrap(True)
        self.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDropIndicatorShown(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.refresh_grid_size()

    def dropEvent(self, event: Any) -> None:  # noqa: N802
        before = self._ordered_names()
        super().dropEvent(event)
        if before != self._ordered_names():
            self.order_changed.emit()

    def refresh_grid_size(self) -> None:
        size = QSize(self._tile_width, self._tile_height)
        self.setGridSize(size)
        for row in range(self.count()):
            self.item(row).setSizeHint(size)

    def _ordered_names(self) -> list[str]:
        return [
            str(self.item(row).data(IMAGE_NAME_ROLE) or self.item(row).text())
            for row in range(self.count())
        ]


class ParamEditorWidget(QWidget):
    """Renders the appropriate input widget for a single command parameter."""

    def __init__(
        self,
        param_def: dict[str, Any],
        value: Any,
        on_change: Callable[[str, Any], None],
        db: Any | None = None,
        parent: QWidget | None = None,
        *,
        capture_target_launcher: Callable[[str], None] | None = None,
        dynamic_target_launcher: Callable[[str], None] | None = None,
        visual_rule_launcher: Callable[[str, str], str | None] | None = None,
        android_package_launcher: Callable[[str], None] | None = None,
        value_catalog: FlowValueCatalog | None = None,
        builtin_types: Mapping[str, ValueType] | None = None,
        create_variable: Callable[[set[ValueType]], str | None] | None = None,
        hook_choices: list[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(parent)
        self._param_def = param_def
        self._param_name: str = param_def["name"]
        self._param_type: str = param_def["type"]
        self._on_change = on_change
        self._db = db
        self._capture_target_launcher = capture_target_launcher
        self._dynamic_target_launcher = dynamic_target_launcher
        self._visual_rule_launcher = visual_rule_launcher
        self._android_package_launcher = android_package_launcher
        self._value_catalog = value_catalog
        self._builtin_types = dict(builtin_types or {})
        self._create_variable = create_variable
        self._hook_choices = list(hook_choices or [])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        label = QLabel(param_def.get("label", self._param_name))
        description = str(param_def.get("description", "")).strip()
        if description:
            label.setToolTip(description)
        label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10pt; "
            "font-weight: 600;"
        )
        layout.addWidget(label)

        display_value = (
            param_def["default"]
            if value is None and "default" in param_def
            else value
        )
        widget = self._create_widget(display_value)
        if description:
            widget.setToolTip(description)
        layout.addWidget(widget)

    def _create_widget(self, value: Any) -> QWidget:
        ptype = self._param_type

        if ptype == "string":
            if self._param_def.get("picker") == "android_package":
                return self._create_android_package_widget(value)
            return self._create_string_widget(value)
        elif ptype == "locator":
            return self._create_locator_widget(value)
        elif ptype == "int":
            return self._create_int_widget(value)
        elif ptype == "double":
            return self._create_double_widget(value)
        elif ptype == "bool":
            return self._create_bool_widget(value)
        elif ptype == "enum":
            return self._create_enum_widget(value)
        elif ptype == "multiline":
            return self._create_multiline_widget(value)
        elif ptype == "image_list":
            return self._create_image_list_widget(value)
        elif ptype == "string_list":
            return self._create_string_list_widget(value)
        elif ptype == "hook_list":
            return self._create_hook_list_widget(value)
        elif ptype == "key_list":
            return self._create_key_list_widget(value)
        elif ptype == "visual_rule":
            return self._create_visual_rule_widget(value)
        elif ptype == "flow_reference":
            return self._create_flow_reference_widget(value)
        elif ptype == "variable_target":
            return self._create_variable_target_widget(value)
        elif ptype == "value_reference":
            return self._create_value_reference_widget(value)
        elif ptype == "value_template":
            return self._create_value_template_widget(value)
        else:
            label = QLabel(f"Unsupported parameter type: {ptype}")
            label.setStyleSheet(f"color: {COLORS['error']};")
            return label

    def _create_value_template_widget(self, value: Any) -> QWidget:
        catalog = self._value_catalog
        if catalog is None:
            label = QLabel("Inputs & Variables are unavailable")
            label.setStyleSheet(f"color: {COLORS['error']};")
            return label
        return ValueTemplateEditorWidget(
            value,
            multiline=bool(self._param_def.get("template_multiline", False)),
            catalog=catalog,
            builtin_types=self._builtin_types,
            on_change=lambda updated: self._on_change(self._param_name, updated),
            placeholder=self._param_def.get("label", ""),
        )

    def _create_hook_list_widget(self, value: Any) -> QWidget:
        selected = set(value if isinstance(value, list) else [])
        widget = QListWidget()
        widget.setObjectName("hookListPicker")
        widget.setMinimumHeight(90)
        for hook_id, name in self._hook_choices:
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, hook_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if hook_id in selected
                else Qt.CheckState.Unchecked
            )
            widget.addItem(item)

        def changed(_item: QListWidgetItem) -> None:
            ordered = [
                str(widget.item(row).data(Qt.ItemDataRole.UserRole))
                for row in range(widget.count())
                if widget.item(row).checkState() == Qt.CheckState.Checked
            ]
            self._on_change(self._param_name, ordered)

        widget.itemChanged.connect(changed)
        return widget

    def _create_variable_target_widget(self, value: Any) -> QWidget:
        combo = NoScrollComboBox()
        combo.setObjectName("variableTargetPicker")
        combo.addItem(
            "Do not store result"
            if self._param_def.get("optional", False)
            else "Select Variable",
            "",
        )
        catalog = self._value_catalog
        if catalog is None:
            if isinstance(value, str) and value:
                combo.addItem(f"Unavailable Variable ({value})", value)
                combo.setCurrentIndex(1)
            else:
                combo.addItem("Inputs & Variables are unavailable", None)
            combo.setEnabled(False)
            return combo
        allowed_type_values = self._param_def.get("allowed_value_types")
        allowed_types = (
            {ValueType(item) for item in allowed_type_values}
            if isinstance(allowed_type_values, list)
            else set(ValueType)
        )
        compatible_variables = tuple(
            variable
            for variable in catalog.variables
            if variable.type in allowed_types
        )
        known_ids = {item.id for item in compatible_variables}
        all_variables = {item.id: item for item in catalog.variables}
        if self._create_variable is not None:
            create_index = combo.count()
            combo.addItem("Create new Variable…", "")
            combo.setItemData(
                create_index,
                True,
                Qt.ItemDataRole.UserRole + 1,
            )
        for variable in compatible_variables:
            combo.addItem(f"{variable.name} ({variable.type.value})", variable.id)
        if isinstance(value, str) and value and value not in known_ids:
            existing = all_variables.get(value)
            if existing is None:
                combo.addItem(f"Missing Variable ({value})", value)
            else:
                combo.addItem(
                    f"Incompatible Variable — {existing.name} "
                    f"({existing.type.value})",
                    value,
                )
        index = combo.findData(value if isinstance(value, str) else "")
        combo.setCurrentIndex(max(0, index))

        def _changed(_index: int) -> None:
            if combo.itemData(_index, Qt.ItemDataRole.UserRole + 1):
                created_id = self._create_variable(allowed_types)
                if created_id:
                    self._on_change(self._param_name, created_id)
                combo.setCurrentIndex(max(0, combo.findData(value)))
                return
            selected = combo.currentData()
            if isinstance(selected, str) and (
                selected or self._param_def.get("optional", False)
            ):
                self._on_change(self._param_name, selected)

        combo.currentIndexChanged.connect(_changed)
        return combo

    def _create_value_reference_widget(self, value: Any) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        picker = NoScrollComboBox()
        picker.setObjectName("valueReferencePicker")
        picker.addItem("Select value", None)
        type_labels = {
            ValueType.TEXT: "Text",
            ValueType.INTEGER: "Integer",
            ValueType.NUMBER: "Number",
            ValueType.BOOLEAN: "Boolean",
            ValueType.DURATION_MS: "Duration",
        }
        allowed_type_values = self._param_def.get("allowed_value_types")
        allowed_types = (
            {ValueType(item) for item in allowed_type_values}
            if isinstance(allowed_type_values, list)
            else set(ValueType)
        )
        if value is not None and not isinstance(value, Mapping) and len(allowed_types) == 1:
            literal_type = next(iter(allowed_types))
            if (
                literal_type is ValueType.DURATION_MS
                and isinstance(value, float)
                and value.is_integer()
            ):
                value = int(value)
            value = {
                "kind": "literal",
                "type": literal_type.value,
                "value": value,
            }
        for value_type in ValueType:
            if value_type not in allowed_types:
                continue
            picker.addItem(
                f"Literal — {type_labels[value_type]}",
                ("literal", value_type.value),
            )
        catalog = self._value_catalog
        if catalog is not None:
            for definition in catalog.inputs:
                if not any(
                    is_value_type_assignable(definition.type, target)
                    for target in allowed_types
                ):
                    continue
                picker.addItem(
                    f"Input — {definition.name} ({definition.type.value})",
                    ("input", definition.id),
                )
            for definition in catalog.variables:
                if not any(
                    is_value_type_assignable(definition.type, target)
                    for target in allowed_types
                ):
                    continue
                picker.addItem(
                    f"Variable — {definition.name} ({definition.type.value})",
                    ("variable", definition.id),
                )
        for name, value_type in sorted(self._builtin_types.items()):
            if any(
                is_value_type_assignable(value_type, target)
                for target in allowed_types
            ):
                picker.addItem(
                    f"Built-in — {name} ({value_type.value})",
                    ("builtin", name),
                )

        value_host = QWidget()
        value_layout = QVBoxLayout(value_host)
        value_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(picker)
        layout.addWidget(value_host)

        existing_literal: LiteralValueReference | None = None
        selected_data: tuple[str, str] | None = None
        selected_index: int | None = None
        if value is not None:
            try:
                reference = value_reference_from_dict(value)
                if isinstance(reference, LiteralValueReference):
                    existing_literal = reference
                    selected_data = ("literal", reference.type.value)
                elif hasattr(reference, "id"):
                    selected_data = (value["kind"], reference.id)
                else:
                    selected_data = ("builtin", reference.name)  # type: ignore[union-attr]
            except (ValueReferenceError, KeyError, TypeError):
                picker.addItem("Invalid saved reference", ("invalid", ""))
                selected_index = picker.count() - 1
                selected_data = ("invalid", "")

        def _clear_value_editor() -> None:
            while value_layout.count():
                item = value_layout.takeAt(0)
                child = item.widget()
                if child is not None:
                    child.deleteLater()

        def _show_selection(*, initializing: bool = False) -> None:
            _clear_value_editor()
            selected = picker.currentData()
            if not isinstance(selected, tuple) or len(selected) != 2:
                return
            kind, identity = selected
            if kind in {"input", "variable", "builtin"}:
                if not initializing:
                    self._on_change(
                        self._param_name,
                        {"kind": kind, "name": identity}
                        if kind == "builtin"
                        else {"kind": kind, "id": identity},
                    )
                return
            if kind != "literal":
                warning = QLabel("This saved reference must be repaired.")
                warning.setStyleSheet(f"color: {COLORS['error']};")
                value_layout.addWidget(warning)
                return
            value_type = ValueType(identity)
            if value_type is ValueType.BOOLEAN:
                boolean_editor = NoScrollComboBox()
                boolean_editor.setObjectName("literalBooleanEditor")
                boolean_editor.setAccessibleName("Boolean literal value")
                boolean_editor.addItem("False", False)
                boolean_editor.addItem("True", True)
                if (
                    initializing
                    and existing_literal is not None
                    and existing_literal.type is ValueType.BOOLEAN
                ):
                    boolean_editor.setCurrentIndex(
                        1 if bool(existing_literal.value) else 0
                    )

                def _commit_boolean() -> None:
                    self._on_change(
                        self._param_name,
                        {
                            "kind": "literal",
                            "type": ValueType.BOOLEAN.value,
                            "value": bool(boolean_editor.currentData()),
                        },
                    )

                boolean_editor.currentIndexChanged.connect(
                    lambda _index: _commit_boolean()
                )
                value_layout.addWidget(boolean_editor)
                if not initializing:
                    _commit_boolean()
                return
            edit = QLineEdit()
            edit.setObjectName("literalValueEditor")
            edit.setPlaceholderText(f"Enter {type_labels[value_type].lower()} value")
            if (
                initializing
                and existing_literal is not None
                and existing_literal.type is value_type
            ):
                edit.setText(canonical_typed_value_text(existing_literal.value))

            def _commit_literal() -> None:
                try:
                    normalized = coerce_typed_value(
                        value_type,
                        edit.text(),
                        subject="Literal value",
                    )
                except ValueCoercionError as exc:
                    edit.setStyleSheet(f"border: 1px solid {COLORS['error']};")
                    edit.setToolTip(str(exc))
                    return
                edit.setStyleSheet("")
                edit.setToolTip("")
                self._on_change(
                    self._param_name,
                    {"kind": "literal", "type": value_type.value, "value": normalized},
                )

            edit.editingFinished.connect(_commit_literal)
            value_layout.addWidget(edit)

        picker.currentIndexChanged.connect(lambda _index: _show_selection())
        if selected_data is not None:
            picker.blockSignals(True)
            index = (
                selected_index
                if selected_index is not None
                else next(
                    (
                        item_index
                        for item_index in range(picker.count())
                        if picker.itemData(item_index) == selected_data
                    ),
                    -1,
                )
            )
            if index < 0:
                kind, identity = selected_data
                if kind == "literal":
                    label = f"Incompatible Literal ({identity})"
                else:
                    if kind == "builtin":
                        known_type = self._builtin_types.get(identity)
                        status = "Incompatible" if known_type is not None else "Out-of-scope"
                    else:
                        declaration_exists = bool(
                            catalog is not None
                            and (
                                catalog.input_by_id(identity)
                                if kind == "input"
                                else catalog.variable_by_id(identity)
                                if kind == "variable"
                                else None
                            )
                        )
                        status = "Incompatible" if declaration_exists else "Missing"
                    kind_label = "Built-in" if kind == "builtin" else kind.title()
                    label = f"{status} {kind_label} ({identity})"
                picker.addItem(label, selected_data)
                index = picker.count() - 1
            picker.setCurrentIndex(index)
            picker.blockSignals(False)
        _show_selection(initializing=True)
        return container

    def _wrap_with_variable_insert(
        self,
        editor: QWidget,
        insert_text: Callable[[str], None],
    ) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(editor)

        insert_row = QHBoxLayout()
        insert_row.setContentsMargins(0, 0, 0, 0)
        insert_row.setSpacing(4)

        combo = NoScrollComboBox()
        combo.addItem("Insert variable", "")
        combo.addItem("Current item", "{{item}}")
        combo.addItem("Current index", "{{index}}")
        combo.setToolTip("Insert a runtime variable token")
        combo.currentIndexChanged.connect(
            lambda _idx: self._insert_selected_variable(combo, insert_text)
        )

        insert_row.addWidget(combo)
        insert_row.addStretch()
        layout.addLayout(insert_row)
        return container

    @staticmethod
    def _insert_selected_variable(
        combo: QComboBox,
        insert_text: Callable[[str], None],
    ) -> None:
        value = combo.currentData()
        if not value:
            return
        insert_text(str(value))
        combo.setCurrentIndex(0)

    def _create_string_widget(self, value: Any) -> QWidget:
        edit = QLineEdit(str(value) if value else "")
        edit.setPlaceholderText(self._param_def.get("label", ""))
        edit.textChanged.connect(lambda text: self._on_change(self._param_name, text))

        def _insert(text: str) -> None:
            edit.insert(text)

        if self._param_def.get("disable_legacy_insert"):
            return edit
        return self._wrap_with_variable_insert(edit, _insert)

    def _create_android_package_widget(self, value: Any) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        edit = QLineEdit(str(value) if value else "")
        edit.setPlaceholderText(self._param_def.get("label", "Package Name"))
        edit.textChanged.connect(lambda text: self._on_change(self._param_name, text))
        layout.addWidget(edit, 1)

        choose = QPushButton("Choose…")
        choose.setEnabled(self._android_package_launcher is not None)
        choose.setToolTip("Browse user-installed apps on the connected Android device")
        if self._android_package_launcher is not None:
            choose.clicked.connect(
                lambda: self._android_package_launcher(self._param_name)
            )
        layout.addWidget(choose)

        if self._param_def.get("disable_legacy_insert"):
            return row
        return self._wrap_with_variable_insert(row, edit.insert)

    def _create_locator_widget(self, value: Any) -> QWidget:
        """Create a compact editor for the canonical browser locator shape."""
        try:
            locator = normalize_locator(value) if value not in (None, "") else {
                "platform": "browser",
                "strategy": LocatorStrategy.CSS.value,
                "value": "",
            }
        except (TypeError, ValueError):
            locator = {
                "platform": "browser",
                "strategy": LocatorStrategy.CSS.value,
                "value": "",
            }

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        strategy = NoScrollComboBox()
        strategy.setObjectName("locatorStrategy")
        for item in LocatorStrategy:
            strategy.addItem(item.value.replace("_", " ").title(), item.value)
        strategy.setCurrentIndex(
            max(0, strategy.findData(locator.get("strategy", "css")))
        )

        value_edit = QLineEdit(str(locator.get("value", "")))
        value_edit.setObjectName("locatorValue")
        value_edit.setPlaceholderText("Selector or accessible text")

        role_row = QWidget()
        role_layout = QHBoxLayout(role_row)
        role_layout.setContentsMargins(0, 0, 0, 0)
        role_layout.setSpacing(4)
        role_edit = QLineEdit(str(locator.get("role", "")))
        role_edit.setObjectName("locatorRole")
        role_edit.setPlaceholderText("Role (for example, button)")
        name_edit = QLineEdit(str(locator.get("name", "")))
        name_edit.setObjectName("locatorName")
        name_edit.setPlaceholderText("Accessible name (optional)")
        role_layout.addWidget(role_edit)
        role_layout.addWidget(name_edit)

        exact = QCheckBox("Exact text/name")
        exact.setObjectName("locatorExact")
        exact.setChecked(bool(locator.get("exact", False)))
        fallbacks = locator.get("fallbacks", [])

        layout.addWidget(strategy)
        layout.addWidget(value_edit)
        layout.addWidget(role_row)
        layout.addWidget(exact)

        exact_strategies = {
            LocatorStrategy.ROLE.value,
            LocatorStrategy.TEXT.value,
            LocatorStrategy.LABEL.value,
            LocatorStrategy.PLACEHOLDER.value,
            LocatorStrategy.ALT_TEXT.value,
            LocatorStrategy.TITLE.value,
        }

        def _emit() -> None:
            selected = str(strategy.currentData())
            payload: dict[str, Any] = {
                "platform": "browser",
                "strategy": selected,
            }
            if selected == LocatorStrategy.ROLE.value:
                payload["role"] = role_edit.text()
                if name_edit.text():
                    payload["name"] = name_edit.text()
            else:
                payload["value"] = value_edit.text()
            if exact.isChecked():
                payload["exact"] = True
            if fallbacks:
                payload["fallbacks"] = fallbacks
            self._on_change(self._param_name, payload)

        def _refresh() -> None:
            selected = str(strategy.currentData())
            role_mode = selected == LocatorStrategy.ROLE.value
            value_edit.setVisible(not role_mode)
            role_row.setVisible(role_mode)
            exact.setVisible(selected in exact_strategies)

        strategy.currentIndexChanged.connect(lambda _index: (_refresh(), _emit()))
        value_edit.textChanged.connect(lambda _text: _emit())
        role_edit.textChanged.connect(lambda _text: _emit())
        name_edit.textChanged.connect(lambda _text: _emit())
        exact.toggled.connect(lambda _checked: _emit())
        _refresh()
        return container

    def _create_int_widget(self, value: Any) -> QWidget:
        spin = NoScrollSpinBox()
        spin.setRange(_int_param_min(self._param_def), _int_param_max(self._param_def))
        spin.setValue(int(value) if value is not None else 0)
        spin.valueChanged.connect(lambda v: self._on_change(self._param_name, v))
        return spin

    def _create_double_widget(self, value: Any) -> QWidget:
        spin = NoScrollDoubleSpinBox()
        name_lower = self._param_name.lower()
        if "min" in self._param_def or "max" in self._param_def:
            spin.setRange(
                float(self._param_def.get("min", 0.0)),
                float(self._param_def.get("max", 99999.0)),
            )
        elif "threshold" in name_lower or name_lower == "color_tolerance":
            spin.setRange(0.0, 1.0)
        else:
            spin.setRange(0.0, 99999.0)
        if "threshold" in name_lower or name_lower == "color_tolerance":
            spin.setSingleStep(0.01)
            spin.setDecimals(2)
        else:
            spin.setSingleStep(0.1)
            spin.setDecimals(2)
        spin.setValue(float(value) if value is not None else 0.0)
        spin.valueChanged.connect(lambda v: self._on_change(self._param_name, v))
        return spin

    def _create_bool_widget(self, value: Any) -> QWidget:
        cb = QCheckBox()
        cb.setChecked(bool(value) if value is not None else False)
        cb.toggled.connect(lambda checked: self._on_change(self._param_name, checked))
        return cb

    def _create_enum_widget(self, value: Any) -> QWidget:
        combo = NoScrollComboBox()
        options = self._param_def.get("options", [])
        option_labels = self._param_def.get("option_labels", {})
        labels = option_labels if isinstance(option_labels, Mapping) else {}
        value_aliases = self._param_def.get("value_aliases", {})
        aliases = value_aliases if isinstance(value_aliases, Mapping) else {}
        for option in options:
            if not isinstance(option, str):
                continue
            combo.addItem(str(labels.get(option, option)), option)
        selected_index = combo.findData(aliases.get(value, value))
        if selected_index >= 0:
            combo.setCurrentIndex(selected_index)
        elif options:
            combo.setCurrentIndex(0)

        def _changed(index: int) -> None:
            selected = combo.itemData(index)
            if isinstance(selected, str):
                self._on_change(self._param_name, selected)

        combo.currentIndexChanged.connect(_changed)
        return combo

    def _create_multiline_widget(self, value: Any) -> QWidget:
        edit = QTextEdit()
        edit.setPlainText(str(value) if value else "")
        edit.setMaximumHeight(100)
        edit.setPlaceholderText(self._param_def.get("label", ""))
        edit.textChanged.connect(
            lambda: self._on_change(self._param_name, edit.toPlainText())
        )

        def _insert(text: str) -> None:
            edit.insertPlainText(text)

        if self._param_def.get("disable_legacy_insert"):
            return edit
        return self._wrap_with_variable_insert(edit, _insert)

    def _coerce_string_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [
                item
                for item in (str(entry).strip() for entry in value if entry is not None)
                if item
            ]

        if isinstance(value, str):
            parts = value.splitlines() if "\n" in value else value.split(",")
        elif value is None:
            parts = []
        else:
            parts = str(value).split(",")

        return [item for item in (part.strip() for part in parts) if item]

    def _create_string_list_widget(self, value: Any) -> QWidget:
        container = QWidget()
        v_layout = QVBoxLayout(container)
        v_layout.setContentsMargins(0, 0, 0, 0)
        v_layout.setSpacing(4)

        list_widget = QListWidget()
        list_widget.setMinimumHeight(112)
        list_widget.setMaximumHeight(150)
        list_widget.setSpacing(3)
        list_widget.setStyleSheet(
            f"QListWidget {{ background-color: {COLORS['bg_darkest']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 5px; padding: 3px; }}"
            f"QListWidget::item {{ border: 1px solid {COLORS['border_light']}; "
            f"border-radius: 5px; padding: 3px 8px; }}"
            f"QListWidget::item:selected {{ background-color: {COLORS['bg_elevated']}; }}"
            f"QListWidget QLineEdit {{ background-color: {COLORS['bg_elevated']}; "
            f"color: {COLORS['text_primary']}; border: 2px solid {COLORS['primary']}; "
            f"selection-background-color: {COLORS['primary']}; "
            f"selection-color: {COLORS['selection_text']}; padding: 2px 6px; }}"
        )
        current_items = self._coerce_string_list(value)
        is_updating = False

        def _current_texts() -> list[str]:
            return [
                item
                for item in (
                    list_widget.item(row).text().strip()
                    for row in range(list_widget.count())
                )
                if item
            ]

        def _make_item(text: str) -> QListWidgetItem:
            item = QListWidgetItem(text)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            item.setSizeHint(QSize(180, 28))
            return item

        def _populate_list() -> None:
            nonlocal is_updating
            is_updating = True
            list_widget.blockSignals(True)
            list_widget.clear()
            for item_text in current_items:
                list_widget.addItem(_make_item(item_text))
            list_widget.blockSignals(False)
            is_updating = False

        def _emit_items() -> None:
            current_items[:] = _current_texts()
            self._on_change(self._param_name, list(current_items))

        def _item_changed(_item: QListWidgetItem) -> None:
            if is_updating:
                return
            _emit_items()

        def _add_item() -> None:
            item = _make_item("")
            list_widget.addItem(item)
            list_widget.setCurrentItem(item)
            list_widget.editItem(item)

        def _remove_item() -> None:
            row = list_widget.currentRow()
            if row < 0:
                return
            list_widget.takeItem(row)
            _emit_items()

        def _move_selected(delta: int) -> None:
            row = list_widget.currentRow()
            target = row + delta
            if row < 0 or target < 0 or target >= list_widget.count():
                return
            item = list_widget.takeItem(row)
            list_widget.insertItem(target, item)
            list_widget.setCurrentRow(target)
            _emit_items()

        def _paste_lines() -> None:
            clipboard = QApplication.clipboard()
            clipboard_text = clipboard.text().strip() if clipboard is not None else ""
            if not clipboard_text:
                return
            pasted_items = _parse_bulk_text(clipboard_text)
            if not pasted_items:
                return
            for item_text in pasted_items:
                list_widget.addItem(_make_item(item_text))
            _emit_items()

        def _copy_all_items() -> None:
            clipboard = QApplication.clipboard()
            if clipboard is None:
                return
            clipboard.setText("\n".join(_current_texts()))

        def _replace_lines() -> None:
            clipboard = QApplication.clipboard()
            clipboard_text = clipboard.text().strip() if clipboard is not None else ""
            if not clipboard_text:
                return
            pasted_items = _parse_bulk_text(clipboard_text)
            if not pasted_items:
                return
            list_widget.clear()
            for item_text in pasted_items:
                list_widget.addItem(_make_item(item_text))
            _emit_items()

        def _clear_items() -> None:
            list_widget.clear()
            _emit_items()

        def _parse_bulk_text(text: str) -> list[str]:
            parsed_items = [
                item for item in (line.strip() for line in text.splitlines()) if item
            ]
            if not parsed_items and "," in text:
                parsed_items = [
                    item for item in (part.strip() for part in text.split(",")) if item
                ]
            elif len(parsed_items) == 1 and "," in parsed_items[0]:
                parsed_items = [
                    item
                    for item in (part.strip() for part in parsed_items[0].split(","))
                    if item
                ]
            return parsed_items

        _populate_list()
        list_widget.itemChanged.connect(_item_changed)
        v_layout.addWidget(list_widget)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)

        add_btn = QPushButton("Add")
        add_btn.setToolTip("Add value")

        remove_btn = QPushButton("Remove")
        remove_btn.setToolTip("Remove selected value")

        up_btn = QPushButton("↑")
        up_btn.setToolTip("Move selected value up")
        up_btn.setFixedWidth(32)

        down_btn = QPushButton("↓")
        down_btn.setToolTip("Move selected value down")
        down_btn.setFixedWidth(32)

        paste_btn = QPushButton("Paste Lines")
        paste_btn.setToolTip("Append newline-separated values from the clipboard")

        copy_btn = QPushButton("Copy All")
        copy_btn.setToolTip("Copy all values as newline-separated text")

        replace_btn = QPushButton("Replace Lines")
        replace_btn.setToolTip("Replace all values with newline-separated clipboard text")

        clear_btn = QPushButton("Clear")
        clear_btn.setToolTip("Remove all values")

        add_btn.clicked.connect(_add_item)
        remove_btn.clicked.connect(_remove_item)
        up_btn.clicked.connect(lambda: _move_selected(-1))
        down_btn.clicked.connect(lambda: _move_selected(1))
        paste_btn.clicked.connect(_paste_lines)
        copy_btn.clicked.connect(_copy_all_items)
        replace_btn.clicked.connect(_replace_lines)
        clear_btn.clicked.connect(_clear_items)

        btn_row.addWidget(add_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addWidget(up_btn)
        btn_row.addWidget(down_btn)
        btn_row.addWidget(paste_btn)
        btn_row.addWidget(copy_btn)
        btn_row.addWidget(replace_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch()
        v_layout.addLayout(btn_row)

        return container

    def _create_image_list_widget(self, value: Any) -> QWidget:
        """Image list with thumbnail display and a picker dialog for adding."""
        container = QWidget()
        v_layout = QVBoxLayout(container)
        v_layout.setContentsMargins(0, 0, 0, 0)
        v_layout.setSpacing(4)

        hint_label = QLabel(
            "Priority order: 1 is checked/clicked first. Drag tiles or use arrows."
        )
        hint_label.setStyleSheet(
            f"color: {COLORS['text_tertiary']}; font-size: 10pt; font-weight: 600;"
        )
        v_layout.addWidget(hint_label)

        list_widget = AutoFitImageListWidget()
        list_widget.setItemDelegate(PriorityImageItemDelegate(list_widget))
        list_widget.setIconSize(QSize(32, 32))
        list_widget.setMinimumHeight(96)
        list_widget.setMaximumHeight(96)
        list_widget.setSpacing(2)
        list_widget.setStyleSheet(
            f"QListWidget {{ background-color: {COLORS['input_bg']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 5px; padding: 3px; }}"
        )
        current_items: list[str] = list(value) if isinstance(value, list) else []

        def _renumber_items() -> None:
            for row in range(list_widget.count()):
                item = list_widget.item(row)
                item.setData(IMAGE_ORDER_ROLE, row + 1)
                item.setToolTip(
                    f"Priority {row + 1}: {item.data(IMAGE_NAME_ROLE)}"
                )

        def _populate_list() -> None:
            list_widget.clear()
            for index, img_name in enumerate(current_items, start=1):
                item = QListWidgetItem(img_name)
                item.setData(IMAGE_NAME_ROLE, img_name)
                item.setData(IMAGE_ORDER_ROLE, index)
                item.setToolTip(f"Priority {index}: {img_name}")
                # Try to load a thumbnail
                if self._db is not None:
                    try:
                        data = self._db.load_image(img_name)
                        if data:
                            pixmap = QPixmap()
                            pixmap.loadFromData(data)
                            thumb = pixmap.scaled(
                                QSize(32, 32),
                                Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation,
                            )
                            item.setIcon(QIcon(thumb))
                    except Exception:
                        pass
                list_widget.addItem(item)
            list_widget.refresh_grid_size()

        def _sync_order_from_list() -> None:
            ordered = list_widget._ordered_names()
            if ordered == current_items:
                return
            current_items[:] = ordered
            _renumber_items()
            self._on_change(self._param_name, list(current_items))

        _populate_list()
        v_layout.addWidget(list_widget)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)

        browse_btn = QPushButton("Browse...")
        browse_btn.setToolTip("Open image picker to add templates")
        browse_btn.setStyleSheet(
            f"QPushButton {{ color: {COLORS['text_primary']}; "
            f"background-color: {COLORS['bg_elevated']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 4px; "
            f"padding: 4px 12px; }}"
        )

        capture_btn: QPushButton | None = None
        record_btn: QPushButton | None = None
        if (
            self._param_def.get("record_dynamic_target")
            and self._dynamic_target_launcher is not None
        ):
            record_btn = QPushButton("Record target...")
            record_btn.setObjectName("recordDynamicTargetButton")
            record_btn.setProperty("cssClass", "primary")
            record_btn.setToolTip(
                "Record a short animation and learn representative target poses"
            )
            record_btn.clicked.connect(
                lambda: self._dynamic_target_launcher(self._param_name)
            )
        if (
            self._capture_target_launcher is not None
            and not self._param_def.get("record_dynamic_target")
        ):
            capture_btn = QPushButton("Capture target...")
            capture_btn.setObjectName("captureTargetButton")
            capture_btn.setProperty("cssClass", "primary")
            capture_btn.setToolTip(
                "Capture or import a screen, select a target, and add it here"
            )
            capture_btn.clicked.connect(
                lambda: self._capture_target_launcher(self._param_name)
            )

        dynamic_target_images = bool(self._param_def.get("record_dynamic_target"))
        remove_btn = QPushButton("Detach" if dynamic_target_images else "Remove")
        remove_btn.setToolTip(
            "Detach the selected pose from this command. Its stored image remains "
            "available in the image library."
            if dynamic_target_images
            else "Remove selected image from list"
        )
        remove_btn.setStyleSheet(
            f"QPushButton {{ color: {COLORS['text_primary']}; "
            f"background-color: {COLORS['bg_elevated']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 4px; "
            f"padding: 4px 12px; }}"
        )

        up_btn = QPushButton("↑")
        up_btn.setToolTip("Move selected image earlier in the priority order")
        up_btn.setFixedWidth(32)
        up_btn.setStyleSheet(
            f"QPushButton {{ color: {COLORS['text_primary']}; "
            f"background-color: {COLORS['bg_elevated']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 4px; "
            f"padding: 4px 8px; }}"
        )

        down_btn = QPushButton("↓")
        down_btn.setToolTip("Move selected image later in the priority order")
        down_btn.setFixedWidth(32)
        down_btn.setStyleSheet(
            f"QPushButton {{ color: {COLORS['text_primary']}; "
            f"background-color: {COLORS['bg_elevated']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 4px; "
            f"padding: 4px 8px; }}"
        )

        clear_btn = QPushButton("Detach all" if dynamic_target_images else "Clear")
        clear_btn.setToolTip(
            "Detach every pose from this command. Stored images are not deleted."
            if dynamic_target_images
            else "Remove all images from list"
        )
        clear_btn.setStyleSheet(
            f"QPushButton {{ color: {COLORS['red']}; "
            f"background-color: {COLORS['bg_elevated']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 4px; "
            f"padding: 4px 8px; }}"
        )

        def _open_picker() -> None:
            from automadin.ui.flow_editor.image_picker import ImagePickerDialog

            dlg = ImagePickerDialog(
                self._db,
                selected=current_items,
                multi_select=True,
                parent=self.window(),
            )
            if dlg.exec() == ImagePickerDialog.DialogCode.Accepted:
                selected = dlg.selected_images()
                selected_set = set(selected)
                current_items[:] = [
                    name for name in current_items if name in selected_set
                ]
                for name in selected:
                    if name not in current_items:
                        current_items.append(name)
                _populate_list()
                self._on_change(self._param_name, list(current_items))

        def _remove_image() -> None:
            row = list_widget.currentRow()
            if row >= 0:
                current_items.pop(row)
                _populate_list()
                self._on_change(self._param_name, list(current_items))

        def _move_selected(delta: int) -> None:
            row = list_widget.currentRow()
            target = row + delta
            if row < 0 or target < 0 or target >= len(current_items):
                return
            current_items.insert(target, current_items.pop(row))
            _populate_list()
            list_widget.setCurrentRow(target)
            self._on_change(self._param_name, list(current_items))

        def _clear_images() -> None:
            current_items.clear()
            _populate_list()
            self._on_change(self._param_name, [])

        list_widget.order_changed.connect(_sync_order_from_list)
        browse_btn.clicked.connect(_open_picker)
        remove_btn.clicked.connect(_remove_image)
        up_btn.clicked.connect(lambda: _move_selected(-1))
        down_btn.clicked.connect(lambda: _move_selected(1))
        clear_btn.clicked.connect(_clear_images)

        if record_btn is not None:
            btn_row.addWidget(record_btn)
        btn_row.addWidget(browse_btn)
        if capture_btn is not None:
            btn_row.addWidget(capture_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addWidget(up_btn)
        btn_row.addWidget(down_btn)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch()
        v_layout.addLayout(btn_row)

        return container

    def _create_key_list_widget(self, value: Any) -> QWidget:
        container = QWidget()
        v_layout = QVBoxLayout(container)
        v_layout.setContentsMargins(0, 0, 0, 0)
        v_layout.setSpacing(4)

        list_widget = QListWidget()
        list_widget.setViewMode(QListView.ViewMode.IconMode)
        list_widget.setFlow(QListView.Flow.LeftToRight)
        list_widget.setResizeMode(QListView.ResizeMode.Adjust)
        list_widget.setWrapping(True)
        list_widget.setMaximumHeight(58)
        list_widget.setSpacing(4)
        list_widget.setStyleSheet(
            f"QListWidget {{ background-color: {COLORS['bg_darkest']}; "
            f"border: 1px solid {COLORS['border']}; border-radius: 5px; padding: 3px; }}"
            f"QListWidget::item {{ border: 1px solid {COLORS['border_light']}; "
            f"border-radius: 5px; padding: 3px 8px; }}"
            f"QListWidget::item:selected {{ background-color: {COLORS['bg_elevated']}; }}"
        )
        current_items: list[str] = list(value) if isinstance(value, list) else []
        for item in current_items:
            list_item = QListWidgetItem(str(item))
            list_item.setSizeHint(QSize(78, 30))
            list_widget.addItem(list_item)
        v_layout.addWidget(list_widget)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)

        key_combo = QComboBox()
        key_combo.setEditable(True)
        common_keys = [
            "Enter", "Tab", "Escape", "Backspace", "Delete",
            "Space", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
            "Home", "End", "PageUp", "PageDown",
            "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
            "Control", "Shift", "Alt", "Meta",
            "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m",
            "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z",
        ]
        key_combo.addItems(common_keys)
        btn_row.addWidget(key_combo, 1)

        add_btn = QPushButton("Add key")
        add_btn.setToolTip("Add selected key")

        remove_btn = QPushButton("Remove")
        remove_btn.setToolTip("Remove selected key")

        def _add_key() -> None:
            key = key_combo.currentText()
            if key:
                current_items.append(key)
                item = QListWidgetItem(key)
                item.setSizeHint(QSize(78, 30))
                list_widget.addItem(item)
                self._on_change(self._param_name, list(current_items))

        def _remove_key() -> None:
            row = list_widget.currentRow()
            if row >= 0:
                current_items.pop(row)
                list_widget.takeItem(row)
                self._on_change(self._param_name, list(current_items))

        add_btn.clicked.connect(_add_key)
        remove_btn.clicked.connect(_remove_key)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(remove_btn)
        v_layout.addLayout(btn_row)

        return container

    def _create_visual_rule_widget(self, value: Any) -> QWidget:
        names = (
            self._db.list_visual_rules()
            if self._db is not None and hasattr(self._db, "list_visual_rules")
            else []
        )
        combo = self._create_resource_reference_combo(
            value,
            names,
            allow_empty=bool(self._param_def.get("optional", False)),
            empty_label="Select a visual rule…",
        )
        if self._visual_rule_launcher is None:
            return combo
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(combo, 1)
        manage = QPushButton("Create / edit / test...")
        manage.setObjectName("visualRuleEditorButton")
        manage.setToolTip("Open the visual rule calibrator and live detection preview")

        def launch() -> None:
            selected = str(combo.currentData() or combo.currentText() or "")
            updated = self._visual_rule_launcher(self._param_name, selected)
            if not updated:
                return
            if combo.findData(updated) < 0:
                combo.addItem(resource_display_label(updated), updated)
            combo.setCurrentIndex(combo.findData(updated))

        manage.clicked.connect(launch)
        layout.addWidget(manage)
        return container

    def _create_flow_reference_widget(self, value: Any) -> QWidget:
        names = (
            self._db.list_flows()
            if self._db is not None and hasattr(self._db, "list_flows")
            else []
        )
        return self._create_resource_reference_combo(
            value,
            names,
            allow_empty=bool(self._param_def.get("optional", False)),
            empty_label="Select a flow…",
        )

    def _create_resource_reference_combo(
        self,
        value: Any,
        names: list[str],
        *,
        allow_empty: bool,
        empty_label: str,
    ) -> QWidget:
        combo = NoScrollComboBox()
        combo.setEditable(False)
        if allow_empty:
            combo.addItem("(none)", "")
        else:
            combo.addItem(empty_label, "")
        folder_index = (
            self._db.workspace_folder_index()
            if self._db is not None
            and hasattr(self._db, "workspace_folder_index")
            else None
        )
        for name in sorted(names, key=resource_sort_key):
            display_path = (
                folder_index.display_path(name)
                if folder_index is not None
                else name
            )
            combo.addItem(resource_display_label(display_path), name)
            combo.setItemData(
                combo.count() - 1,
                display_path,
                Qt.ItemDataRole.ToolTipRole,
            )

        current = str(value) if value else ""
        index = combo.findData(current)
        if current and index < 0:
            display_current = (
                folder_index.display_path(current)
                if folder_index is not None
                else current
            )
            combo.addItem(
                f"{resource_display_label(display_current)} (missing)",
                current,
            )
            index = combo.findData(current)
        if index >= 0:
            combo.setCurrentIndex(index)
        elif not allow_empty and combo.count() > 0:
            combo.setCurrentIndex(0)

        combo.currentIndexChanged.connect(
            lambda _idx: self._on_change(self._param_name, combo.currentData() or "")
        )
        return combo
