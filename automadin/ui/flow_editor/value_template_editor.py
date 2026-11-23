"""Stable-ID value-template editing for version-1 command text fields."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QLineEdit,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from automadin.models.flow_value import (
    FlowInputDefinition,
    FlowValueCatalog,
    FlowVariableDefinition,
)
from automadin.models.typed_value import ValueType
from automadin.models.value_reference import (
    BuiltinValueReference,
    FlowValueTemplate,
    InputValueReference,
    TemplateSegment,
    TemplateTextSegment,
    ValueReferenceError,
    VariableValueReference,
)
from automadin.ui.theme import COLORS

_TOKEN = re.compile(r"\{\{([^{}]+)\}\}")


def parse_value_template_editor_text(
    text: str,
    catalog: FlowValueCatalog,
    builtin_types: Mapping[str, ValueType],
) -> FlowValueTemplate:
    """Convert readable picker tokens to a strict stable-ID template."""
    if "{{" in _TOKEN.sub("", text) or "}}" in _TOKEN.sub("", text):
        raise ValueReferenceError("Value tokens must use the form {{name}}.")
    segments: list[TemplateSegment] = []
    cursor = 0
    for match in _TOKEN.finditer(text):
        if match.start() > cursor:
            segments.append(TemplateTextSegment(text[cursor : match.start()]))
        token = match.group(1)
        qualifier, separator, name = token.partition(":")
        if not separator:
            name = qualifier
            qualifier = ""
        declaration = catalog.readable_by_name(name)
        if qualifier in {"", "input"} and isinstance(
            declaration, FlowInputDefinition
        ):
            segments.append(InputValueReference(declaration.id))
        elif qualifier in {"", "variable"} and isinstance(
            declaration, FlowVariableDefinition
        ):
            segments.append(VariableValueReference(declaration.id))
        elif qualifier in {"", "builtin"} and name in builtin_types:
            segments.append(BuiltinValueReference(name))
        else:
            raise ValueReferenceError(
                f"Value token '{{{{{token}}}}}' is missing or unavailable in this scope."
            )
        cursor = match.end()
    if cursor < len(text):
        segments.append(TemplateTextSegment(text[cursor:]))
    return FlowValueTemplate(tuple(segments))


def _render_editor_template(
    template: FlowValueTemplate,
    catalog: FlowValueCatalog,
) -> str:
    parts: list[str] = []
    for segment in template.segments:
        if isinstance(segment, TemplateTextSegment):
            parts.append(segment.text)
        elif isinstance(segment, InputValueReference):
            declaration = catalog.input_by_id(segment.id)
            parts.append(
                f"{{{{input:{declaration.name}}}}}"
                if declaration is not None
                else f"{{{{missing-input:{segment.id}}}}}"
            )
        elif isinstance(segment, VariableValueReference):
            declaration = catalog.variable_by_id(segment.id)
            parts.append(
                f"{{{{variable:{declaration.name}}}}}"
                if declaration is not None
                else f"{{{{missing-variable:{segment.id}}}}}"
            )
        else:
            parts.append(f"{{{{builtin:{segment.name}}}}}")
    return "".join(parts)


class ValueTemplateEditorWidget(QWidget):
    """Edit readable text while persisting references as structured segments."""

    def __init__(
        self,
        value: Any,
        *,
        multiline: bool,
        catalog: FlowValueCatalog,
        builtin_types: Mapping[str, ValueType],
        on_change: Callable[[object], None],
        placeholder: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._catalog = catalog
        self._builtin_types = dict(builtin_types)
        self._on_change = on_change
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        if multiline:
            editor = QTextEdit()
            editor.setMaximumHeight(100)
            editor.setPlaceholderText(placeholder)
            editor.textChanged.connect(self._editor_changed)
        else:
            editor = QLineEdit()
            editor.setPlaceholderText(placeholder)
            editor.textChanged.connect(self._editor_changed)
        editor.setObjectName("valueTemplateTextEditor")
        self._editor = editor
        layout.addWidget(editor)

        self._picker = QComboBox()
        self._picker.setObjectName("insertValuePicker")
        self._picker.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._picker.addItem("Insert Value", None)
        for definition in catalog.inputs:
            self._picker.addItem(
                f"Input — {definition.name} ({definition.type.value})",
                ("input", definition.name),
            )
        for definition in catalog.variables:
            self._picker.addItem(
                f"Variable — {definition.name} ({definition.type.value})",
                ("variable", definition.name),
            )
        for name, value_type in sorted(self._builtin_types.items()):
            self._picker.addItem(
                f"Built-in — {name} ({value_type.value})",
                ("builtin", name),
            )
        self._picker.currentIndexChanged.connect(self._insert_selected)
        layout.addWidget(self._picker)

        self._error = QLabel()
        self._error.setObjectName("valueTemplateError")
        self._error.setWordWrap(True)
        self._error.setStyleSheet(f"color: {COLORS['error']};")
        self._error.setVisible(False)
        layout.addWidget(self._error)

        initial_text = ""
        if isinstance(value, str):
            initial_text = value
        elif value is not None:
            try:
                initial_text = _render_editor_template(
                    FlowValueTemplate.from_dict(value),
                    catalog,
                )
            except ValueReferenceError as exc:
                self._set_error(f"Invalid saved value template: {exc}")
        self._set_text(initial_text)

    def _text(self) -> str:
        if isinstance(self._editor, QTextEdit):
            return self._editor.toPlainText()
        return self._editor.text()

    def _set_text(self, text: str) -> None:
        self._editor.blockSignals(True)
        if isinstance(self._editor, QTextEdit):
            self._editor.setPlainText(text)
        else:
            self._editor.setText(text)
        self._editor.blockSignals(False)

    def _set_error(self, message: str) -> None:
        self._error.setText(message)
        self._error.setVisible(bool(message))

    def _insert_selected(self, _index: int) -> None:
        selected = self._picker.currentData()
        if not isinstance(selected, (list, tuple)) or len(selected) != 2:
            return
        kind, name = selected
        if not isinstance(kind, str) or not isinstance(name, str):
            return
        token = f"{{{{{kind}:{name}}}}}"
        if isinstance(self._editor, QTextEdit):
            self._editor.insertPlainText(token)
        else:
            self._editor.insert(token)
        self._picker.blockSignals(True)
        self._picker.setCurrentIndex(0)
        self._picker.blockSignals(False)

    def _editor_changed(self, *_args: object) -> None:
        text = self._text()
        if "{{" not in text and "}}" not in text:
            self._set_error("")
            self._on_change(text)
            return
        try:
            template = parse_value_template_editor_text(
                text,
                self._catalog,
                self._builtin_types,
            )
        except ValueReferenceError as exc:
            self._set_error(str(exc))
            return
        self._set_error("")
        self._on_change(template.to_dict())


__all__ = [
    "ValueTemplateEditorWidget",
    "parse_value_template_editor_text",
]
