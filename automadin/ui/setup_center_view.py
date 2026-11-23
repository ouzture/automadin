"""Persistent, target-aware onboarding and readiness surface."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from automadin.models.device import AndroidDevice, BrowserDevice, WindowsDevice
from automadin.services.browser_health import BrowserHealthState
from automadin.services.browser_health_cache import (
    BrowserHealthCache,
    default_browser_health_cache,
)
from automadin.services.database import DatabaseService
from automadin.services.setup_center import (
    CURRENT_SETUP_VERSION,
    SetupCenterService,
    SetupTrack,
)
from automadin.services.setup_readiness import (
    SetupReadinessCancelled,
    SetupReadinessSnapshot,
    collect_setup_readiness,
)
from automadin.services.qt_worker_lifecycle import (
    qthread_start_may_have_succeeded,
)
from automadin.ui.theme import COLORS

logger = logging.getLogger(__name__)

_READINESS_FAILURE_MESSAGE = (
    "Setup status could not be refreshed. Check the Automadin log, then retry."
)


class SetupRowStatus(str, Enum):
    CHECKING = "Checking…"
    READY = "Ready"
    READY_WITH_LIMITS = "Ready with limits"
    NEEDS_ACTION = "Needs action"
    BLOCKED = "Blocked"
    OPTIONAL = "Optional"
    COULD_NOT_CHECK = "Could not check"


@dataclass(frozen=True, slots=True)
class SetupRowModel:
    item_id: str
    title: str
    status: SetupRowStatus
    detail: str
    action_id: str | None = None
    action_label: str | None = None
    required: bool = True


def _runtime_detail(label: str, status: Any) -> str:
    if status.usable:
        source = str(status.source).replace("_", " ")
        version = f" · {status.version}" if status.version else ""
        return f"{label} detected from {source}{version}."
    return status.error or f"{label} was not detected."


def _counted(count: int, singular: str, plural: str | None = None) -> str:
    noun = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {noun}"


def _browser_tools_row(
    snapshot: SetupReadinessSnapshot,
    selected_track: SetupTrack | None,
) -> SetupRowModel:
    required = selected_track is SetupTrack.BROWSER
    health = dict(snapshot.browser_health)
    target_statuses = (
        ("chromium", "Chromium", snapshot.playwright),
        ("firefox", "Firefox", snapshot.playwright),
        ("webkit", "WebKit", snapshot.playwright),
        ("chrome", "Google Chrome", snapshot.chrome),
        ("msedge", "Microsoft Edge", snapshot.edge),
    )
    launch_tested: list[str] = []
    detected_only: list[str] = []
    needs_attention: list[str] = []
    unavailable: list[str] = []
    could_not_check: list[str] = []

    for target, label, runtime_status in target_statuses:
        result = health.get(target)
        if runtime_status.source == "error":
            could_not_check.append(label)
        elif result is None:
            (detected_only if runtime_status.usable else unavailable).append(label)
        elif result.state is BrowserHealthState.HEALTHY:
            launch_tested.append(label)
        elif result.state is BrowserHealthState.UNAVAILABLE:
            unavailable.append(label)
        elif result.state in {
            BrowserHealthState.FAILED,
            BrowserHealthState.CLEANUP_FAILED,
        }:
            needs_attention.append(label)
        else:
            could_not_check.append(label)

    details: list[str] = []
    for prefix, labels in (
        ("Launch tested", launch_tested),
        ("Detected — not launch tested", detected_only),
        ("Needs attention", needs_attention),
        ("Could not check", could_not_check),
        ("Unavailable", unavailable),
    ):
        if labels:
            details.append(f"{prefix}: {', '.join(labels)}.")
    if snapshot.browser_health_error:
        details.append(
            "The launch-health service could not complete: "
            f"{snapshot.browser_health_error}"
        )
    if health:
        details.append(
            "Launch tests use isolated temporary browser data and no external "
            "navigation. They do not verify headed mode, profile storage, account "
            "sign-in, websites, or future launches."
        )
    elif detected_only:
        details.append(
            "Detection confirms files or an executable are present; it does not "
            "prove the browser can launch."
        )

    if launch_tested:
        limited = bool(
            detected_only or needs_attention or could_not_check or unavailable
        )
        row_status = (
            SetupRowStatus.READY_WITH_LIMITS if limited else SetupRowStatus.READY
        )
        action_label = "Manage tools"
    elif needs_attention or detected_only:
        row_status = (
            SetupRowStatus.NEEDS_ACTION if required else SetupRowStatus.OPTIONAL
        )
        action_label = "Review tools"
    elif could_not_check or snapshot.browser_health_error:
        row_status = SetupRowStatus.COULD_NOT_CHECK
        action_label = "Review tools"
    else:
        row_status = (
            SetupRowStatus.NEEDS_ACTION if required else SetupRowStatus.OPTIONAL
        )
        action_label = "Set up browsers"

    return SetupRowModel(
        "browser_tools",
        "Browser tools",
        row_status,
        " ".join(details)
        or "No Playwright browser payload, Google Chrome, or Microsoft Edge was detected.",
        "runtime",
        action_label,
        required,
    )


def _android_tools_row(
    snapshot: SetupReadinessSnapshot,
    selected_track: SetupTrack | None,
) -> SetupRowModel:
    required = selected_track is SetupTrack.ANDROID
    if snapshot.adb.usable:
        return SetupRowModel(
            "android_tools",
            "Android tools (ADB)",
            SetupRowStatus.READY,
            _runtime_detail("ADB", snapshot.adb)
            + " This confirms ADB responds, not that a device is connected.",
            "runtime",
            "Manage tools",
            required,
        )
    if snapshot.adb.source == "error":
        status = SetupRowStatus.COULD_NOT_CHECK
    else:
        status = SetupRowStatus.NEEDS_ACTION if required else SetupRowStatus.OPTIONAL
    return SetupRowModel(
        "android_tools",
        "Android tools (ADB)",
        status,
        snapshot.adb.error or "No usable ADB executable was detected.",
        "runtime",
        "Set up ADB",
        required,
    )


def _windows_tools_row(selected_track: SetupTrack | None) -> SetupRowModel:
    required = selected_track is SetupTrack.WINDOWS
    if sys.platform == "win32":
        return SetupRowModel(
            "windows_tools",
            "Windows app automation",
            SetupRowStatus.READY,
            "Windows automation is available through visible application windows.",
            required=required,
        )
    return SetupRowModel(
        "windows_tools",
        "Windows app automation",
        SetupRowStatus.BLOCKED if required else SetupRowStatus.OPTIONAL,
        "Windows app automation is available only when Automadin runs on Windows.",
        required=required,
    )


def _ocr_row(snapshot: SetupReadinessSnapshot) -> SetupRowModel:
    """Present OCR honestly without making it a prerequisite for every user."""
    status = snapshot.ocr
    if status is None:
        return SetupRowModel(
            "screen_text",
            "Screen text recognition (OCR)",
            SetupRowStatus.COULD_NOT_CHECK,
            "OCR availability could not be checked.",
            "ocr",
            "Set up OCR",
            False,
        )
    if status.usable:
        return SetupRowModel(
            "screen_text",
            "Screen text recognition (OCR)",
            SetupRowStatus.READY,
            f"Tesseract is ready ({status.version or 'version unknown'}; "
            f"{status.source}). Enabled languages: {', '.join(status.selected_languages)}.",
            "ocr",
            "Manage OCR",
            False,
        )
    return SetupRowModel(
        "screen_text",
        "Screen text recognition (OCR)",
        SetupRowStatus.OPTIONAL,
        status.error or "Tesseract is not configured.",
        "ocr",
        "Set up OCR",
        False,
    )


def build_setup_rows(
    snapshot: SetupReadinessSnapshot | None,
    selected_track: SetupTrack | None,
    connected_track: SetupTrack | None,
    connected_label: str | None,
    assessment_error: str | None = None,
) -> tuple[SetupRowModel, ...]:
    """Build presentation-neutral checklist rows from current facts."""
    if snapshot is None and assessment_error:
        failed_rows = [
            SetupRowModel(
                "foundation",
                "Data and version",
                SetupRowStatus.COULD_NOT_CHECK,
                assessment_error,
                "refresh",
                "Retry",
            )
        ]
        for item_id, title, required in (
            (
                "browser_tools",
                "Browser tools",
                selected_track is SetupTrack.BROWSER,
            ),
            (
                "android_tools",
                "Android tools (ADB)",
                selected_track is SetupTrack.ANDROID,
            ),
            (
                "windows_tools",
                "Windows app automation",
                selected_track is SetupTrack.WINDOWS,
            ),
            ("screen_text", "Screen text recognition (OCR)", False),
            (
                "browser_profile",
                "Browser profile storage",
                selected_track is SetupTrack.BROWSER,
            ),
            ("target", "Target connection", True),
            ("flow", "Automation workspace", True),
        ):
            failed_rows.append(
                SetupRowModel(
                    item_id,
                    title,
                    SetupRowStatus.COULD_NOT_CHECK,
                    "Readiness was not updated. Retry the check for current details.",
                    required=required,
                )
            )
        return tuple(failed_rows)

    if snapshot is None:
        checking = SetupRowModel(
            "foundation",
            "Data and version",
            SetupRowStatus.CHECKING,
            "Checking the active workspace and data location…",
        )
        rows: list[SetupRowModel] = [checking]
        for item_id, title in (
            ("browser_tools", "Browser tools"),
            ("android_tools", "Android tools (ADB)"),
            ("windows_tools", "Windows app automation"),
            ("screen_text", "Screen text recognition (OCR)"),
            ("browser_profile", "Browser profile storage"),
            ("target", "Target connection"),
            ("flow", "Automation workspace"),
        ):
            required = item_id in {"target", "flow"}
            if selected_track is SetupTrack.BROWSER:
                required = required or item_id in {
                    "browser_tools",
                    "browser_profile",
                }
            elif selected_track is SetupTrack.ANDROID:
                required = required or item_id == "android_tools"
            elif selected_track is SetupTrack.WINDOWS:
                required = required or item_id == "windows_tools"
            rows.append(
                SetupRowModel(
                    item_id,
                    title,
                    SetupRowStatus.CHECKING,
                    "Waiting for the readiness check…",
                    required=required,
                )
            )
        return tuple(rows)

    foundation_errors = [
        error
        for error in (snapshot.data_error, snapshot.schema_error)
        if error
    ]
    if snapshot.data_writable and snapshot.schema_version is not None:
        foundation = SetupRowModel(
            "foundation",
            "Data and version",
            SetupRowStatus.READY,
            (
                f"Automadin {snapshot.app_version} · database schema "
                f"{snapshot.schema_version} · writable data at {snapshot.data_dir}"
            ),
        )
    elif snapshot.data_writable is False:
        foundation = SetupRowModel(
            "foundation",
            "Data and version",
            SetupRowStatus.BLOCKED,
            " ".join(foundation_errors) or "The active data directory is not writable.",
            "backup_help",
            "View data help",
        )
    else:
        foundation = SetupRowModel(
            "foundation",
            "Data and version",
            SetupRowStatus.COULD_NOT_CHECK,
            " ".join(foundation_errors) or "Workspace readiness could not be checked.",
            "refresh",
            "Retry",
        )

    if snapshot.profile_error:
        profile = SetupRowModel(
            "browser_profile",
            "Browser profile storage",
            SetupRowStatus.COULD_NOT_CHECK,
            snapshot.profile_error,
            "profiles",
            "Review profiles",
            selected_track is SetupTrack.BROWSER,
        )
    elif snapshot.profile_ready:
        attention = max(
            0,
            (snapshot.profile_total or snapshot.profile_ready) - snapshot.profile_ready,
        )
        detail = (
            f"{_counted(snapshot.profile_ready, 'reusable browser profile')} "
            f"{'is' if snapshot.profile_ready == 1 else 'are'} storage-ready."
        )
        if attention:
            detail += (
                f" {_counted(attention, 'additional profile')} "
                f"{'needs' if attention == 1 else 'need'} attention."
            )
        detail += " Storage readiness does not guarantee the account is still signed in."
        profile = SetupRowModel(
            "browser_profile",
            "Browser profile storage",
            SetupRowStatus.READY,
            detail,
            "profiles",
            "Manage profiles",
            selected_track is SetupTrack.BROWSER,
        )
    else:
        required = selected_track is SetupTrack.BROWSER
        count = snapshot.profile_total or 0
        detail = (
            f"{_counted(count, 'profile')} "
            f"{'exists' if count == 1 else 'exist'}, but none has reusable local storage."
            if count
            else (
                "No reusable browser profile is configured. Temporary browser "
                "sessions remain available but discard their data."
            )
        )
        profile = SetupRowModel(
            "browser_profile",
            "Browser profile storage",
            SetupRowStatus.NEEDS_ACTION if required else SetupRowStatus.OPTIONAL,
            detail,
            "profiles",
            "Manage profiles",
            required,
        )

    if selected_track is None:
        target = SetupRowModel(
            "target",
            "Target connection",
            SetupRowStatus.NEEDS_ACTION,
            "Choose what you want to automate first, then test that target.",
        )
    elif connected_track is selected_track:
        target = SetupRowModel(
            "target",
            "Target connection",
            SetupRowStatus.READY,
            (
                f"Connected now: {connected_label or selected_track.value}. "
                "Use Disconnect in the target bar before choosing another."
            ),
        )
    elif connected_track is not None:
        target = SetupRowModel(
            "target",
            "Target connection",
            SetupRowStatus.NEEDS_ACTION,
            (
                f"The current connection is {connected_label or connected_track.value}. "
                "Disconnect it in the target bar before finding a "
                f"{selected_track.value} target."
            ),
        )
    else:
        detail = f"No {selected_track.value} target is connected."
        target = SetupRowModel(
            "target",
            "Target connection",
            SetupRowStatus.NEEDS_ACTION,
            detail,
            f"target_{selected_track.value}",
            "Find a target",
        )

    if snapshot.workspace_error or snapshot.flow_count is None:
        flow = SetupRowModel(
            "flow",
            "Automation workspace",
            SetupRowStatus.COULD_NOT_CHECK,
            snapshot.workspace_error or "Saved flows could not be checked.",
            "flows",
            "Open flows",
        )
    elif snapshot.flow_count:
        run_note = (
            f" {_counted(snapshot.run_config_count, 'saved run setup')} "
            f"{'is' if snapshot.run_config_count == 1 else 'are'} available."
            if snapshot.run_config_count
            else ""
        )
        flow = SetupRowModel(
            "flow",
            "Automation workspace",
            SetupRowStatus.READY,
            (
                f"{_counted(snapshot.flow_count, 'saved flow')} "
                f"{'is' if snapshot.flow_count == 1 else 'are'} available.{run_note} "
                "This does not infer target compatibility; Automadin validates "
                "flows before save and execution."
            ),
            "flows",
            "Open flows",
        )
    else:
        flow = SetupRowModel(
            "flow",
            "Automation workspace",
            SetupRowStatus.NEEDS_ACTION,
            "Create a flow or restore a workspace. Preflight will guide repairs before execution.",
            "flows",
            "Create a flow",
        )

    return (
        foundation,
        _browser_tools_row(snapshot, selected_track),
        _android_tools_row(snapshot, selected_track),
        _windows_tools_row(selected_track),
        _ocr_row(snapshot),
        profile,
        target,
        flow,
    )


def _setup_stylesheet() -> str:
    return f"""
        QWidget#setupCenter {{
            background-color: {COLORS["bg_darkest"]};
        }}
        QWidget#setupCanvas {{
            background-color: {COLORS["bg_darkest"]};
        }}
        QLabel#setupEyebrow {{
            color: {COLORS["primary_hover"]};
            font-size: 10.5pt;
            font-weight: 700;
        }}
        QLabel#setupTitle {{
            color: {COLORS["text_primary"]};
            font-size: 24pt;
            font-weight: 700;
        }}
        QLabel#setupSubtitle {{
            color: {COLORS["text_secondary"]};
            font-size: 11pt;
        }}
        QLabel#setupMeta {{
            color: {COLORS["text_tertiary"]};
            font-size: 10.5pt;
        }}
        QLabel#setupSection {{
            color: {COLORS["text_primary"]};
            font-size: 13pt;
            font-weight: 700;
        }}
        QPushButton[setupTrack="true"] {{
            min-height: 42px;
            padding: 0 18px;
            text-align: left;
            background-color: {COLORS["bg_surface"]};
            border: 1px solid {COLORS["border"]};
            color: {COLORS["text_secondary"]};
            font-size: 10.5pt;
        }}
        QPushButton[setupTrack="true"]:hover {{
            background-color: {COLORS["bg_hover"]};
            color: {COLORS["text_primary"]};
        }}
        QPushButton[setupTrack="true"]:checked {{
            background-color: {COLORS["bg_elevated"]};
            border: 2px solid {COLORS["primary"]};
            color: {COLORS["text_primary"]};
            font-weight: 700;
        }}
        QFrame[setupRow="true"] {{
            background-color: transparent;
            border: none;
            border-bottom: 1px solid {COLORS["border_light"]};
        }}
        QLabel[setupRowTitle="true"] {{
            color: {COLORS["text_primary"]};
            font-size: 11.5pt;
            font-weight: 600;
        }}
        QLabel[setupRowDetail="true"] {{
            color: {COLORS["text_secondary"]};
            font-size: 10.5pt;
        }}
        QLabel[setupRowStatus="true"] {{
            font-size: 10.5pt;
            font-weight: 700;
            min-width: 112px;
        }}
        QPushButton[setupRowAction="true"] {{
            min-height: 36px;
            min-width: 136px;
            font-size: 10.5pt;
        }}
        QFrame#setupFooter {{
            background-color: {COLORS["bg_surface"]};
            border: 1px solid {COLORS["border_light"]};
            border-radius: 8px;
        }}
    """


_STATUS_PRESENTATION: dict[SetupRowStatus, tuple[str, str]] = {
    SetupRowStatus.CHECKING: ("…", "text_secondary"),
    SetupRowStatus.READY: ("✓", "status_success"),
    SetupRowStatus.READY_WITH_LIMITS: ("!", "status_warning"),
    SetupRowStatus.NEEDS_ACTION: ("!", "status_warning"),
    SetupRowStatus.BLOCKED: ("×", "status_error"),
    SetupRowStatus.OPTIONAL: ("—", "status_optional"),
    SetupRowStatus.COULD_NOT_CHECK: ("?", "status_error"),
}


class _SetupCheckRow(QFrame):
    action_requested = Signal(str)

    def __init__(self, model: SetupRowModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.item_id = model.item_id
        self._action_button: QPushButton | None = None
        self.setProperty("setupRow", True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 15, 4, 15)
        layout.setSpacing(18)

        symbol, color_token = _STATUS_PRESENTATION[model.status]
        status = QLabel(f"{symbol}  {model.status.value}")
        status.setProperty("setupRowStatus", True)
        status.setStyleSheet(f"color: {COLORS[color_token]};")
        status.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(status)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(4)
        title = QLabel(model.title)
        title.setProperty("setupRowTitle", True)
        detail = QLabel(model.detail)
        detail.setProperty("setupRowDetail", True)
        detail.setWordWrap(True)
        detail.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        text_layout.addWidget(title)
        text_layout.addWidget(detail)
        layout.addLayout(text_layout, 1)

        if model.action_id and model.action_label:
            action = QPushButton(model.action_label)
            action.setProperty("setupRowAction", True)
            action.setCursor(Qt.CursorShape.PointingHandCursor)
            action.setAccessibleName(f"{model.action_label}: {model.title}")
            action.clicked.connect(
                lambda _checked=False, action_id=model.action_id: (
                    self.action_requested.emit(action_id)
                )
            )
            self._action_button = action
            layout.addWidget(action, 0, Qt.AlignmentFlag.AlignVCenter)

        self.setAccessibleName(
            f"{model.title}. {model.status.value}. {model.detail}"
        )

    def restore_focus(self) -> None:
        (self._action_button or self).setFocus(Qt.FocusReason.OtherFocusReason)


class _SetupReadinessThread(QThread):
    completed = Signal(int, object)
    failed = Signal(int, str)

    def __init__(
        self,
        generation: int,
        database: DatabaseService,
        data_dir: Path,
        configured_adb_path: str | Path | None,
        parent: QWidget | None = None,
        *,
        browser_health_cache: BrowserHealthCache | None = None,
    ) -> None:
        super().__init__(parent)
        self.generation = generation
        self._database = database
        self._data_dir = data_dir
        self._configured_adb_path = configured_adb_path
        self._browser_health_cache = browser_health_cache

    def run(self) -> None:
        try:
            snapshot = collect_setup_readiness(
                self._database,
                self._data_dir,
                configured_adb_path=self._configured_adb_path,
                cancel=self.isInterruptionRequested,
                browser_health_cache=self._browser_health_cache,
            )
        except SetupReadinessCancelled:
            return
        except Exception:
            if not self.isInterruptionRequested():
                logger.exception("Setup readiness assessment failed")
                self.failed.emit(self.generation, _READINESS_FAILURE_MESSAGE)
        else:
            if not self.isInterruptionRequested():
                self.completed.emit(self.generation, snapshot)
        finally:
            # DatabaseService assigns a lazy SQLite connection to each calling
            # thread. A new QThread is used per refresh, so release that connection
            # here instead of retaining one handle for every completed assessment.
            try:
                self._database.release_current_thread_connection()
            except Exception:
                logger.exception(
                    "Could not release Setup Center worker database connection"
                )


class SetupCenterView(QWidget):
    """A persistent readiness console that delegates mutations to MainWindow."""

    action_requested = Signal(str)

    def __init__(
        self,
        database: DatabaseService,
        data_dir: str | Path,
        *,
        configured_adb_path: str | Path | None = None,
        browser_health_cache: BrowserHealthCache | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("setupCenter")
        self._database = database
        self._data_dir = Path(data_dir).expanduser().absolute()
        self._configured_adb_path = configured_adb_path
        self._browser_health_cache = (
            browser_health_cache or default_browser_health_cache()
        )
        self._state = SetupCenterService(database)
        preferences = self._state.load_preferences()
        self._selected_track = preferences.selected_track
        self._connected_track: SetupTrack | None = None
        self._connected_label: str | None = None
        self._snapshot: SetupReadinessSnapshot | None = None
        self._refresh_error: str | None = None
        self._thread: _SetupReadinessThread | None = None
        self._generation = 0
        self._refresh_pending = False
        self._fresh_browser_health_pending = False
        self._shutting_down = False
        self._shutdown_wait_pending = False
        self._row_widgets: list[_SetupCheckRow] = []
        self._track_buttons: dict[SetupTrack, QPushButton] = {}

        self._build_ui()
        self.setStyleSheet(_setup_stylesheet())
        self._render()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        canvas = QWidget()
        canvas.setObjectName("setupCanvas")
        canvas_outer = QHBoxLayout(canvas)
        canvas_outer.setContentsMargins(32, 28, 32, 36)
        canvas_outer.setSpacing(0)
        canvas_outer.addStretch(1)

        content = QWidget()
        content.setMaximumWidth(1040)
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        top = QHBoxLayout()
        top.setSpacing(18)
        heading = QVBoxLayout()
        heading.setSpacing(4)
        eyebrow = QLabel(f"SETUP CENTER · CHECKLIST {CURRENT_SETUP_VERSION}")
        eyebrow.setObjectName("setupEyebrow")
        title = QLabel("Ready your workspace")
        title.setObjectName("setupTitle")
        subtitle = QLabel(
            "Choose your first target. Automadin checks current readiness without "
            "treating optional tools as requirements."
        )
        subtitle.setObjectName("setupSubtitle")
        subtitle.setWordWrap(True)
        heading.addWidget(eyebrow)
        heading.addWidget(title)
        heading.addWidget(subtitle)
        top.addLayout(heading, 1)

        self._refresh_button = QPushButton("Refresh status")
        self._refresh_button.setMinimumHeight(40)
        self._refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_button.clicked.connect(
            lambda _checked=False: self.refresh(fresh_browser_health=True)
        )
        top.addWidget(self._refresh_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(top)

        self._summary_label = QLabel("")
        self._summary_label.setObjectName("setupMeta")
        self._summary_label.setAccessibleName("Setup readiness summary")
        layout.addWidget(self._summary_label)

        target_heading = QLabel("What do you want to automate first?")
        target_heading.setObjectName("setupSection")
        layout.addWidget(target_heading)
        target_help = QLabel(
            "This choice controls which checks need action. You can change it at any time."
        )
        target_help.setObjectName("setupMeta")
        target_help.setWordWrap(True)
        layout.addWidget(target_help)

        target_row = QHBoxLayout()
        target_row.setSpacing(10)
        target_group = QButtonGroup(self)
        target_group.setExclusive(True)
        for track, label in (
            (SetupTrack.BROWSER, "Browser"),
            (SetupTrack.ANDROID, "Android / ADB"),
            (SetupTrack.WINDOWS, "Windows app"),
        ):
            button = QPushButton(label)
            button.setProperty("setupTrack", True)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAccessibleName(f"Use {label} as the first automation target")
            button.clicked.connect(
                lambda _checked=False, selected=track: self._select_track(selected)
            )
            target_group.addButton(button)
            target_row.addWidget(button, 1)
            self._track_buttons[track] = button
        layout.addLayout(target_row)

        checklist_heading = QLabel("Current readiness")
        checklist_heading.setObjectName("setupSection")
        layout.addWidget(checklist_heading)
        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        layout.addLayout(self._rows_layout)

        restore_row = QHBoxLayout()
        restore_row.setSpacing(10)
        restore_note = QLabel("Already have an Automadin workspace?")
        restore_note.setObjectName("setupMeta")
        restore_row.addWidget(restore_note, 1)
        restore = QPushButton("Restore portable backup")
        restore.clicked.connect(
            lambda _checked=False: self.action_requested.emit("restore_portable")
        )
        restore_row.addWidget(restore)
        database = QPushButton("Import database only")
        database.clicked.connect(
            lambda _checked=False: self.action_requested.emit("import_database")
        )
        restore_row.addWidget(database)
        layout.addLayout(restore_row)

        footer = QFrame()
        footer.setObjectName("setupFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(16, 12, 16, 12)
        footer_layout.setSpacing(12)
        footer_text = QLabel(
            "Setup remains available here after you continue. Use Refresh after "
            "changing tools in another window."
        )
        footer_text.setObjectName("setupMeta")
        footer_text.setWordWrap(True)
        footer_layout.addWidget(footer_text, 1)
        continue_button = QPushButton("Continue to Flows")
        continue_button.setProperty("cssClass", "primary")
        continue_button.setMinimumHeight(40)
        continue_button.clicked.connect(self._continue_to_flows)
        footer_layout.addWidget(continue_button)
        layout.addWidget(footer)

        canvas_outer.addWidget(content, 1)
        canvas_outer.addStretch(1)
        scroll.setWidget(canvas)
        root.addWidget(scroll)

    @property
    def selected_track(self) -> SetupTrack | None:
        return self._selected_track

    @property
    def snapshot(self) -> SetupReadinessSnapshot | None:
        return self._snapshot

    def should_auto_open(self) -> bool:
        return self._state.should_auto_open()

    def _select_track(self, track: SetupTrack) -> None:
        self._selected_track = track
        self._state.set_selected_track(track)
        self._render()

    def _continue_to_flows(self) -> None:
        self.action_requested.emit("continue_flows")

    def acknowledge_current(self) -> None:
        """Acknowledge this checklist after the navigation owner has continued."""
        self._state.acknowledge_current()

    def set_configured_adb_path(self, path: str | Path | None) -> None:
        self._configured_adb_path = path

    def set_connected_device(self, device: object | None) -> None:
        if isinstance(device, BrowserDevice):
            track = SetupTrack.BROWSER
        elif isinstance(device, AndroidDevice):
            track = SetupTrack.ANDROID
        elif isinstance(device, WindowsDevice):
            track = SetupTrack.WINDOWS
        else:
            track = None
        self.set_connected_target(track, getattr(device, "name", None))

    def set_connected_target(
        self,
        track: SetupTrack | None,
        label: str | None = None,
    ) -> None:
        self._connected_track = track
        self._connected_label = label
        self._render()

    def refresh(self, *, fresh_browser_health: bool = False) -> None:
        if self._shutting_down:
            return
        self._fresh_browser_health_pending = (
            self._fresh_browser_health_pending or fresh_browser_health
        )
        thread = self._thread
        if thread is not None and thread.isRunning():
            self._refresh_pending = True
            self._refresh_button.setText("Refresh queued")
            return
        self._start_refresh()

    def _start_refresh(self) -> None:
        if self._fresh_browser_health_pending:
            self._browser_health_cache.invalidate()
            self._fresh_browser_health_pending = False
        self._generation += 1
        generation = self._generation
        self._refresh_pending = False
        self._refresh_error = None
        self._refresh_button.setEnabled(False)
        self._refresh_button.setText("Checking…")
        if self._snapshot is None:
            self._render()
        thread: _SetupReadinessThread | None = None
        try:
            thread = _SetupReadinessThread(
                generation,
                self._database,
                self._data_dir,
                self._configured_adb_path,
                self,
                browser_health_cache=self._browser_health_cache,
            )
            thread.completed.connect(self._readiness_completed)
            thread.failed.connect(self._readiness_failed)
            thread.finished.connect(self._readiness_thread_finished)
            self._thread = thread
            thread.start()
        except Exception:
            logger.exception("Could not start Setup Center readiness worker")
            running = qthread_start_may_have_succeeded(thread)
            if running:
                # A start implementation may raise after the native thread was
                # created. Keep the Python owner until ``finished`` and reject any
                # late result from this uncertain generation.
                self._generation += 1
                self._refresh_pending = False
                self._refresh_error = _READINESS_FAILURE_MESSAGE
                self._refresh_button.setText("Stopping failed check…")
                thread.requestInterruption()
                self._render()
                return
            if self._thread is thread:
                self._thread = None
            if thread is not None:
                try:
                    thread.deleteLater()
                except Exception:
                    logger.exception(
                        "Could not release Setup Center worker after start failure"
                    )
            self._refresh_pending = False
            self._refresh_error = _READINESS_FAILURE_MESSAGE
            self._refresh_button.setEnabled(True)
            self._refresh_button.setText("Refresh status")
            self._render()

    def _readiness_completed(
        self,
        generation: int,
        snapshot: SetupReadinessSnapshot,
    ) -> None:
        if (
            self._shutting_down
            or generation != self._generation
            or self._refresh_pending
        ):
            return
        self._snapshot = snapshot
        self._refresh_error = None
        self._render()

    def _readiness_failed(self, generation: int, error: str) -> None:
        if (
            self._shutting_down
            or generation != self._generation
            or self._refresh_pending
        ):
            return
        self._refresh_error = error
        self._render()

    def _readiness_thread_finished(self) -> None:
        """Handle worker completion on this view's GUI-thread affinity."""
        thread = self.sender()
        if thread is not None:
            self._readiness_finished(thread)

    def _readiness_finished(self, thread: Any) -> None:
        if self._thread is not thread:
            self._release_readiness_thread(thread)
            return
        self._thread = None
        self._release_readiness_thread(thread)
        if self._shutdown_wait_pending:
            self._shutdown_wait_pending = False
            self._shutting_down = False
            self.setEnabled(True)
            self._summary_label.setText(
                "Readiness check stopped. You can continue or close safely."
            )
            return
        if self._shutting_down:
            return
        if self._refresh_pending:
            self._start_refresh()
            return
        self._refresh_button.setEnabled(True)
        self._refresh_button.setText("Refresh status")

    @staticmethod
    def _release_readiness_thread(thread: Any) -> None:
        try:
            thread.deleteLater()
        except Exception:
            logger.exception("Could not release Setup Center readiness worker")

    def _render(self) -> None:
        focus_widget = QApplication.focusWidget()
        focused_item_id = next(
            (
                row.item_id
                for row in self._row_widgets
                if focus_widget is not None
                and (focus_widget is row or row.isAncestorOf(focus_widget))
            ),
            None,
        )
        for track, button in self._track_buttons.items():
            button.setChecked(track is self._selected_track)

        rows = build_setup_rows(
            self._snapshot,
            self._selected_track,
            self._connected_track,
            self._connected_label,
            self._refresh_error,
        )
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._row_widgets.clear()
        for model in rows:
            row = _SetupCheckRow(model)
            row.action_requested.connect(self._handle_row_action)
            self._rows_layout.addWidget(row)
            self._row_widgets.append(row)
            if row.item_id == focused_item_id:
                row.restore_focus()

        required = [row for row in rows if row.required]
        ready = sum(
            row.status
            in {SetupRowStatus.READY, SetupRowStatus.READY_WITH_LIMITS}
            for row in required
        )
        action = sum(
            row.status
            in {
                SetupRowStatus.NEEDS_ACTION,
                SetupRowStatus.BLOCKED,
                SetupRowStatus.COULD_NOT_CHECK,
            }
            for row in required
        )
        optional = sum(not row.required for row in rows)
        checked = (
            self._snapshot.checked_at.astimezone().strftime("%H:%M:%S")
            if self._snapshot is not None
            else "not checked yet"
        )
        if self._refresh_error:
            self._summary_label.setText(
                "Could not refresh setup status. "
                f"{self._refresh_error} · Last successful check {checked}"
            )
        else:
            self._summary_label.setText(
                f"{ready} of {len(required)} relevant checks ready · "
                f"{action} need action · {optional} optional · Last checked {checked}"
            )

    def _handle_row_action(self, action_id: str) -> None:
        if action_id == "refresh":
            self.refresh(fresh_browser_health=True)
            return
        self.action_requested.emit(action_id)

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        if self._snapshot is None and self._thread is None:
            self.refresh()

    def shutdown(self, wait_ms: int = 0) -> bool:
        """Stop accepting refreshes and retain a running worker until it exits."""
        thread = self._thread
        if thread is None:
            return True
        self._shutting_down = True
        self._refresh_pending = False
        self.setEnabled(False)
        self._shutdown_wait_pending = True
        if thread.isRunning():
            thread.requestInterruption()
        # Retain the worker until its queued ``finished`` handler runs. ``wait_ms``
        # remains accepted for caller compatibility but the GUI thread never waits.
        _ = wait_ms
        return False
