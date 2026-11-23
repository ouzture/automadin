"""Modeless browser element picker for locator-backed flow steps."""

from __future__ import annotations

import time
from typing import Any

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from automadin.models.locator import LocatorSpec
from automadin.ui.theme import COLORS


def _field(value: object, name: str, default: Any = "") -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _candidate_locator(candidate: object) -> dict[str, object]:
    raw = _field(candidate, "locator", candidate)
    if isinstance(raw, LocatorSpec):
        return raw.to_dict()
    return LocatorSpec.parse(raw).to_dict()


class _PickerThread(QThread):
    picked = Signal(object)
    failed = Signal(str)

    def __init__(self, handle: object, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._handle = handle

    def run(self) -> None:
        delivered = False
        try:
            self._handle.start_element_picker()  # type: ignore[attr-defined]
            deadline = time.monotonic() + 60.0
            while not self.isInterruptionRequested() and time.monotonic() < deadline:
                result = self._handle.poll_element_picker()  # type: ignore[attr-defined]
                if result is not None:
                    self.picked.emit(result)
                    delivered = True
                    return
                self.msleep(75)
            if not self.isInterruptionRequested():
                self.failed.emit("No element was selected within 60 seconds.")
        except Exception as exc:
            self.failed.emit(str(exc) or "The browser element picker failed.")
        finally:
            try:
                result = self._handle.stop_element_picker()  # type: ignore[attr-defined]
                if result is not None and not delivered:
                    self.picked.emit(result)
            except Exception:
                pass


class _InspectThread(QThread):
    inspected = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        handle: object,
        locator: dict[str, object],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._handle = handle
        self._locator = locator

    def run(self) -> None:
        try:
            self.inspected.emit(
                self._handle.inspect_locator(self._locator)  # type: ignore[attr-defined]
            )
        except Exception as exc:
            self.failed.emit(str(exc) or "The locator could not be tested.")


class WebInspectorDialog(QDialog):
    """Pick a DOM element and use its locator in a browser flow step."""

    use_requested = Signal(str, dict)

    _DEFAULT_EXACT_ACTIONS = (
        ("click_element", "Click Element"),
        ("wait_for_selector", "Wait For Element"),
        ("input_fill", "Input Fill"),
        ("extract_text", "Extract Text"),
        ("assert_visible", "Assert Visible"),
    )

    def __init__(
        self,
        browser_handle: object,
        parent: QWidget | None = None,
        *,
        locator_actions: tuple[tuple[str, str], ...] | None = None,
    ) -> None:
        super().__init__(parent)
        self._handle = browser_handle
        self._locator_actions = (
            self._DEFAULT_EXACT_ACTIONS
            if locator_actions is None
            else locator_actions
        )
        self._worker: QThread | None = None
        self._close_requested = False

        self.setWindowTitle("Web Inspector")
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(560, 520)
        self.setMinimumSize(460, 420)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title = QLabel("Pick a web element")
        title.setStyleSheet(
            f"color: {COLORS['text_primary']}; font-size: 16pt; font-weight: 700;"
        )
        layout.addWidget(title)

        guidance = QLabel(
            "Start picking, then hover and click an element in the connected browser. "
            "Automadin ranks stable locators and shows how many elements each matches."
        )
        guidance.setWordWrap(True)
        guidance.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10.5pt;"
        )
        layout.addWidget(guidance)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Selector mode"))
        self._mode = QComboBox()
        self._mode.setObjectName("selectorMode")
        self._mode.addItem("Exact element", "exact")
        self._mode.addItem("Element collection", "collection")
        self._mode.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self._mode, 1)
        layout.addLayout(mode_row)

        self._pick_button = QPushButton("Start picking")
        self._pick_button.setProperty("cssClass", "primary")
        self._pick_button.clicked.connect(self._start_picker)
        layout.addWidget(self._pick_button, 0, Qt.AlignmentFlag.AlignLeft)

        self._status = QLabel("Ready")
        self._status.setAccessibleName("Web Inspector status")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._element = QLabel("No element selected")
        self._element.setWordWrap(True)
        self._element.setStyleSheet(
            f"color: {COLORS['text_primary']}; font-size: 11pt; font-weight: 600; "
            f"padding-top: 8px; border-top: 1px solid {COLORS['border']};"
        )
        layout.addWidget(self._element)

        locator_label = QLabel("Locator candidates")
        locator_label.setStyleSheet(
            f"color: {COLORS['text_secondary']}; font-size: 10pt; font-weight: 650;"
        )
        layout.addWidget(locator_label)

        self._candidates = QListWidget()
        self._candidates.setAccessibleName("Locator candidates")
        self._candidates.currentItemChanged.connect(self._on_candidate_changed)
        layout.addWidget(self._candidates, 1)

        use_row = QHBoxLayout()
        use_row.addWidget(QLabel("Use selected locator for"))
        self._action = QComboBox()
        self._action.setObjectName("locatorAction")
        self._action.currentIndexChanged.connect(self._update_actions)
        use_row.addWidget(self._action, 1)
        layout.addLayout(use_row)

        actions = QHBoxLayout()
        actions.addStretch()
        self._test_button = QPushButton("Test locator")
        self._test_button.clicked.connect(self._test_locator)
        actions.addWidget(self._test_button)
        self._add_button = QPushButton("Add click step")
        self._add_button.setProperty("cssClass", "primary")
        self._add_button.clicked.connect(self._add_step)
        actions.addWidget(self._add_button)
        layout.addLayout(actions)
        self._refresh_action_choices()
        self._update_actions()

    def _start_picker(self) -> None:
        if self._worker is not None:
            return
        self._candidates.clear()
        self._element.setText("Move to the browser and select an element")
        self._set_status("Picking is active in the connected browser…")
        worker = _PickerThread(self._handle, self)
        worker.picked.connect(self._on_picked)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        self._update_actions()
        worker.start()

    def _test_locator(self) -> None:
        locator = self._selected_locator()
        if locator is None or self._worker is not None:
            return
        self._set_status("Testing locator uniqueness…")
        worker = _InspectThread(self._handle, locator, self)
        worker.inspected.connect(self._on_inspected)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        self._update_actions()
        worker.start()

    def _on_picked(self, picked: object) -> None:
        metadata = _field(picked, "metadata", {})
        parts = (
            str(_field(metadata, "tag_name", "element")).strip(),
            str(_field(metadata, "role")).strip(),
            str(_field(metadata, "accessible_name")).strip(),
        )
        self._element.setText(" · ".join(part for part in parts if part) or "Element selected")
        self._candidates.clear()
        for candidate in tuple(_field(picked, "candidates", ()) or ()):
            locator = _candidate_locator(candidate)
            count = int(_field(candidate, "count", 0) or 0)
            target = str(locator.get("name") or locator.get("value") or locator.get("role"))
            suffix = "unique" if count == 1 else f"{count} matches"
            guidance = self._stability_guidance(locator)
            item = QListWidgetItem(
                f"{locator['strategy']}: {target}  —  {suffix} · {guidance}"
            )
            item.setData(Qt.ItemDataRole.UserRole, locator)
            item.setData(Qt.ItemDataRole.UserRole + 1, count)
            self._candidates.addItem(item)
        if self._candidates.count():
            self._select_recommended_candidate()
            self._set_status("Element selected. Review or test the locator before adding it.")
        else:
            self._set_status("No replayable locator was produced.", error=True)

    def _on_inspected(self, inspection: object) -> None:
        count = int(_field(inspection, "count", 0) or 0)
        if count == 1:
            self._set_status("Locator is unique on the current page.")
        elif count == 0:
            self._set_status("Locator no longer matches an element.", error=True)
        else:
            self._set_status(f"Locator is ambiguous: it matches {count} elements.", error=True)

    def _on_failed(self, message: str) -> None:
        self._set_status(message, error=True)

    def _on_worker_finished(self) -> None:
        worker = self.sender()
        if isinstance(worker, QThread):
            worker.deleteLater()
        if worker is self._worker:
            self._worker = None
        self._update_actions()
        if self._close_requested:
            self.close()

    def _on_candidate_changed(self, _current: object, _previous: object) -> None:
        self._update_actions()

    def _on_mode_changed(self, _index: int) -> None:
        self._select_recommended_candidate()
        self._refresh_action_choices()
        self._update_actions()

    def _refresh_action_choices(self) -> None:
        previous = self._action.currentData()
        self._action.blockSignals(True)
        self._action.clear()
        choices = (
            (("for_each_browser_element", "For Each Browser Element"),)
            if self._mode.currentData() == "collection"
            else self._locator_actions
        )
        for command_id, label in choices:
            self._action.addItem(label, command_id)
        if previous is not None:
            previous_index = self._action.findData(previous)
            if previous_index >= 0:
                self._action.setCurrentIndex(previous_index)
        self._action.blockSignals(False)

    def _select_recommended_candidate(self) -> None:
        if not self._candidates.count():
            return
        collection = self._mode.currentData() == "collection"
        preferred = next(
            (
                index
                for index in range(self._candidates.count())
                if (
                    self._candidates.item(index).data(Qt.ItemDataRole.UserRole + 1)
                    > 1
                )
                == collection
            ),
            0,
        )
        self._candidates.setCurrentRow(preferred)

    @staticmethod
    def _stability_guidance(locator: dict[str, object]) -> str:
        strategy = locator.get("strategy")
        value = str(locator.get("value") or "")
        if strategy == "test_id":
            return "stable data attribute"
        if strategy == "role":
            return "accessible role/name"
        if strategy == "css" and value.startswith("#"):
            return "element ID"
        if strategy == "css" and "[href=" in value:
            return "link destination"
        if strategy == "css" and value.startswith("."):
            return "class collection"
        if strategy in {"label", "placeholder", "alt_text", "title"}:
            return "semantic attribute"
        if strategy == "text":
            return "visible text may change"
        return "structural selector may be fragile"

    def _selected_locator(self) -> dict[str, object] | None:
        item = self._candidates.currentItem()
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return dict(value) if isinstance(value, dict) else None

    def _add_step(self) -> None:
        locator = self._selected_locator()
        command_id = self._action.currentData()
        if locator is None or not isinstance(command_id, str):
            return
        self.use_requested.emit(command_id, locator)
        self.accept()

    def _update_actions(self, *_args: object) -> None:
        busy = self._worker is not None
        has_locator = self._selected_locator() is not None
        self._pick_button.setEnabled(not busy)
        self._pick_button.setText(
            "Picking…" if isinstance(self._worker, _PickerThread) else "Start picking"
        )
        self._test_button.setEnabled(not busy and has_locator)
        has_action = self._action.currentData() is not None
        self._action.setEnabled(not busy and self._action.count() > 1)
        self._add_button.setEnabled(not busy and has_locator and has_action)
        action_label = self._action.currentText().strip()
        self._add_button.setText(
            f"Add {action_label}" if action_label else "Add step"
        )

    def _set_status(self, message: str, *, error: bool = False) -> None:
        color = COLORS["error"] if error else COLORS["text_secondary"]
        self._status.setStyleSheet(f"color: {color};")
        self._status.setText(message)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._worker is not None and self._worker.isRunning():
            self._close_requested = True
            self._worker.requestInterruption()
            self._set_status("Stopping the inspector…")
            self._update_actions()
            event.ignore()
            return
        event.accept()
