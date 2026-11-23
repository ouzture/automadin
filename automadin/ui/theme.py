"""Theme module for Automadin."""

from __future__ import annotations

DEFAULT_THEME_NAME = "Midnight Neon"

_THEMES: dict[str, dict[str, dict[str, str] | list[str]]] = {
    "Midnight Neon": {
        "colors": {
            "bg_darkest": "#080D1A",
            "bg_surface": "#10192B",
            "bg_elevated": "#1B2943",
            "bg_card": "#152139",
            "bg_hover": "#263A60",
            "input_bg": "#0C1425",
            "input_disabled": "#111827",
            "border": "#46618F",
            "border_light": "#293D62",
            "text_primary": "#F7F8FF",
            "text_secondary": "#C9D4EB",
            "text_tertiary": "#8296BA",
            "text_disabled": "#8296BA",
            "text_on_accent": "#FFFFFF",
            "primary_text": "#FFFFFF",
            "success_text": "#FFFFFF",
            "warning_text": "#FFFFFF",
            "error_text": "#FFFFFF",
            "primary": "#5E54C7",
            "primary_hover": "#6B61D8",
            "success": "#197A4A",
            "success_hover": "#20834F",
            "warning": "#9A681F",
            "warning_hover": "#9B6A22",
            "error": "#B94055",
            "error_hover": "#C94B61",
            "purple": "#B36CFF",
            "pink": "#F062C0",
            "orange": "#FF8A4C",
            "green": "#31C875",
            "yellow": "#F0C94F",
            "blue": "#4DA3FF",
            "red": "#F45B69",
            "cyan": "#29D3E8",
            "accent": "#29D3E8",
            "focus_ring": "#74E8FF",
            "selection_bg": "#4030A8",
            "selection_border": "#9B8CFF",
            "selection_text": "#FFFFFF",
            "canvas_dark": "#050814",
            "overlay_click": "#FF9F43",
            "overlay_match": "#35E07B",
            "overlay_best": "#B8FF7A",
            "status_success": "#4EE38F",
            "status_warning": "#FFD06A",
            "status_error": "#FF7B86",
            "status_optional": "#B7A7FF",
        },
        "category_colors": {
            "ORGANIZATION": "#8296BA",
            "ACTIONS": "#4DA3FF",
            "DETECTION": "#35E07B",
            "CONTROL_FLOW": "#F062C0",
            "TIMING": "#F0C94F",
            "ANDROID": "#29D3E8",
            "BROWSER": "#9B7BFF",
            "WINDOWS": "#FF8A4C",
        },
        "strip_colors": [
            "#4DA3FF",
            "#FF8A4C",
            "#35E07B",
            "#F062C0",
            "#F0C94F",
            "#9B7BFF",
            "#29D3E8",
            "#F45B69",
        ],
    },
    "Solar Paper": {
        "colors": {
            "bg_darkest": "#FFF8E8",
            "bg_surface": "#F2E1BD",
            "bg_elevated": "#FFFFFF",
            "bg_card": "#FFFDF7",
            "bg_hover": "#E8D09E",
            "input_bg": "#FFFFFF",
            "input_disabled": "#E9DFC9",
            "border": "#87683D",
            "border_light": "#CBB58A",
            "text_primary": "#24160A",
            "text_secondary": "#594027",
            "text_tertiary": "#82684D",
            "text_disabled": "#6B563B",
            "text_on_accent": "#FFFFFF",
            "primary_text": "#FFFFFF",
            "success_text": "#FFFFFF",
            "warning_text": "#24160A",
            "error_text": "#FFFFFF",
            "primary": "#9F3B73",
            "primary_hover": "#87305F",
            "success": "#2E7148",
            "success_hover": "#245F3C",
            "warning": "#C17A2B",
            "warning_hover": "#D08B3A",
            "error": "#B73A3A",
            "error_hover": "#9C3030",
            "purple": "#6D3CC3",
            "pink": "#C52E75",
            "orange": "#C55A11",
            "green": "#187A3D",
            "yellow": "#9A6700",
            "blue": "#006BB3",
            "red": "#C62828",
            "cyan": "#007E87",
            "accent": "#007E87",
            "focus_ring": "#007E87",
            "selection_bg": "#FFD166",
            "selection_border": "#B83280",
            "selection_text": "#24160A",
            "canvas_dark": "#21160D",
            "overlay_click": "#D35400",
            "overlay_match": "#138A45",
            "overlay_best": "#5C8A00",
            "status_success": "#116834",
            "status_warning": "#8A3F00",
            "status_error": "#A3162D",
            "status_optional": "#6240A8",
        },
        "category_colors": {
            "ORGANIZATION": "#6B6257",
            "ACTIONS": "#006BB3",
            "DETECTION": "#187A3D",
            "CONTROL_FLOW": "#C52E75",
            "TIMING": "#9A6700",
            "ANDROID": "#007E87",
            "BROWSER": "#6D3CC3",
            "WINDOWS": "#C55A11",
        },
        "strip_colors": [
            "#006BB3",
            "#C55A11",
            "#187A3D",
            "#C52E75",
            "#9A6700",
            "#6D3CC3",
            "#007E87",
            "#C62828",
        ],
    },
}

_AVAILABLE_THEME_NAMES = ("Midnight Neon", "Solar Paper")

COLORS: dict[str, str] = {}
CATEGORY_COLORS: dict[str, str] = {}
CATEGORY_BG_TINTS: dict[str, str] = {}
CATEGORY_HEADER_TINTS: dict[str, str] = {}
STRIP_COLORS: list[str] = []
_active_theme_name = DEFAULT_THEME_NAME


def tint_color(hex_color: str, bg: str | None = None, alpha: float = 0.08) -> str:
    """Blend *hex_color* into *bg* at *alpha* to produce a subtle tint."""
    if bg is None:
        bg = COLORS["bg_surface"]

    def _hex(c: str) -> tuple[int, int, int]:
        c = c.lstrip("#")
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)

    fr, fg, fb = _hex(hex_color)
    br, bg_, bb = _hex(bg)
    r = int(br + (fr - br) * alpha)
    g = int(bg_ + (fg - bg_) * alpha)
    b = int(bb + (fb - bb) * alpha)
    return f"#{r:02x}{g:02x}{b:02x}"


def available_themes() -> list[str]:
    """Return theme names in display order."""
    return list(_AVAILABLE_THEME_NAMES)


def get_active_theme_name() -> str:
    """Return the active theme name."""
    return _active_theme_name


def _update_derived_tints() -> None:
    CATEGORY_BG_TINTS.clear()
    CATEGORY_BG_TINTS.update(
        {cat: tint_color(color, alpha=0.05) for cat, color in CATEGORY_COLORS.items()}
    )

    CATEGORY_HEADER_TINTS.clear()
    CATEGORY_HEADER_TINTS.update(
        {cat: tint_color(color, alpha=0.12) for cat, color in CATEGORY_COLORS.items()}
    )


def set_active_theme(name: str | None) -> str:
    """Apply a named theme to the global theme dictionaries.

    Existing modules import the dictionaries directly, so update them in place.
    """
    global _active_theme_name
    if name not in _AVAILABLE_THEME_NAMES:
        name = DEFAULT_THEME_NAME

    theme = _THEMES[name]
    colors = theme["colors"]

    COLORS.clear()
    COLORS.update(colors)  # type: ignore[arg-type]
    CATEGORY_COLORS.clear()
    CATEGORY_COLORS.update(theme["category_colors"])  # type: ignore[arg-type]
    STRIP_COLORS.clear()
    STRIP_COLORS.extend(theme["strip_colors"])  # type: ignore[arg-type]
    _active_theme_name = name
    _update_derived_tints()
    return _active_theme_name


set_active_theme(DEFAULT_THEME_NAME)


def get_stylesheet() -> str:
    """Return a comprehensive Qt stylesheet for the entire application."""
    c = COLORS
    selection_bg = c["selection_bg"]
    selection_border = c["selection_border"]
    selection_text = c["selection_text"]
    return f"""
        /* ── Base widgets ─────────────────────────────────────────── */

        QMainWindow,
        QWidget {{
            background-color: {c["bg_darkest"]};
            color: {c["text_primary"]};
            font-family: "Segoe UI Variable", "Segoe UI", sans-serif;
            font-size: 10.5pt;
        }}

        /* ── Buttons ──────────────────────────────────────────────── */

        QPushButton {{
            background-color: {c["bg_elevated"]};
            color: {c["text_primary"]};
            border: 1px solid {c["text_tertiary"]};
            border-radius: 6px;
            padding: 7px 14px;
            font-weight: 500;
            min-height: 20px;
        }}

        QPushButton[buttonDensity="compact"] {{
            padding: 4px 10px;
        }}

        QPushButton:hover {{
            background-color: {c["bg_hover"]};
            border-color: {c["primary"]};
        }}

        QPushButton:pressed {{
            background-color: {c["bg_surface"]};
            border-color: {c["primary"]};
        }}

        QPushButton:focus {{
            border: 2px solid {c["accent"]};
            padding: 6px 13px;
        }}

        QPushButton[buttonDensity="compact"]:focus {{
            padding: 3px 9px;
        }}

        QPushButton[buttonAppearance="outline"] {{
            background-color: transparent;
            color: {c["text_secondary"]};
            border-color: {c["border"]};
        }}

        QPushButton[buttonAppearance="outline"]:hover {{
            background-color: {c["bg_elevated"]};
            color: {c["text_primary"]};
        }}

        QPushButton[buttonAppearance="outline"][cssClass="danger"] {{
            background-color: transparent;
            color: {c["error"]};
            border-color: {c["error"]};
        }}

        QPushButton[buttonAppearance="outline"][cssClass="danger"]:hover {{
            background-color: {c["error"]};
            color: {c["error_text"]};
            border-color: {c["error"]};
        }}

        QPushButton[cssClass="primary"]:focus,
        QPushButton[cssClass="danger"]:focus,
        QPushButton[cssClass="success"]:focus,
        QPushButton[cssClass="warning"]:focus {{
            border-color: {c["focus_ring"]};
        }}

        QPushButton[cssClass="primary"] {{
            background-color: {c["primary"]};
            color: {c["primary_text"]};
            border-color: {c["primary"]};
        }}

        QPushButton[cssClass="primary"]:hover {{
            background-color: {c["primary_hover"]};
            border-color: {c["primary_hover"]};
        }}

        QPushButton[cssClass="danger"] {{
            background-color: {c["error"]};
            color: {c["error_text"]};
            border-color: {c["error"]};
        }}

        QPushButton[cssClass="danger"]:hover {{
            background-color: {c["error_hover"]};
            border-color: {c["error_hover"]};
        }}

        QPushButton[cssClass="success"] {{
            background-color: {c["success"]};
            color: {c["success_text"]};
            border-color: {c["success"]};
        }}

        QPushButton[cssClass="success"]:hover {{
            background-color: {c["success_hover"]};
            border-color: {c["success_hover"]};
        }}

        QPushButton[cssClass="warning"] {{
            background-color: {c["warning"]};
            color: {c["warning_text"]};
            border-color: {c["warning"]};
        }}

        QPushButton[cssClass="warning"]:hover {{
            background-color: {c["warning_hover"]};
            border-color: {c["warning_hover"]};
        }}

        QPushButton:disabled,
        QPushButton[cssClass="primary"]:disabled,
        QPushButton[cssClass="danger"]:disabled,
        QPushButton[cssClass="success"]:disabled,
        QPushButton[cssClass="warning"]:disabled {{
            background-color: {c["input_disabled"]};
            color: {c["text_disabled"]};
            border-color: {c["border_light"]};
        }}

        /* ── Text inputs ──────────────────────────────────────────── */

        QLineEdit,
        QTextEdit,
        QPlainTextEdit {{
            background-color: {c["bg_elevated"]};
            color: {c["text_primary"]};
            border: 1px solid {c["text_tertiary"]};
            border-radius: 6px;
            padding: 7px 9px;
            selection-background-color: {c["primary"]};
            selection-color: {c["text_on_accent"]};
        }}

        QLineEdit:focus,
        QTextEdit:focus,
        QPlainTextEdit:focus {{
            border: 2px solid {c["primary"]};
            padding: 6px 8px;
        }}

        QLineEdit:disabled,
        QTextEdit:disabled,
        QPlainTextEdit:disabled {{
            color: {c["text_tertiary"]};
            background-color: {c["input_disabled"]};
        }}

        /* ── Spin boxes ───────────────────────────────────────────── */

        QSpinBox,
        QDoubleSpinBox {{
            background-color: {c["bg_elevated"]};
            color: {c["text_primary"]};
            border: 1px solid {c["text_tertiary"]};
            border-radius: 6px;
            padding: 6px 8px;
            min-height: 28px;
        }}

        QSpinBox:focus,
        QDoubleSpinBox:focus {{
            border: 2px solid {c["primary"]};
            padding: 5px 7px;
        }}

        /* ── Combo box ────────────────────────────────────────────── */

        QComboBox {{
            background-color: {c["bg_elevated"]};
            color: {c["text_primary"]};
            border: 1px solid {c["text_tertiary"]};
            border-radius: 6px;
            padding: 6px 9px;
            min-height: 28px;
        }}

        QComboBox:focus {{
            border: 2px solid {c["primary"]};
            padding: 5px 8px;
        }}

        QComboBox[controlDensity="compact"] {{
            padding: 4px 10px;
            min-height: 20px;
        }}

        QComboBox[controlDensity="compact"]:focus {{
            padding: 3px 9px;
        }}

        QComboBox QAbstractItemView {{
            background-color: {c["input_bg"]};
            color: {c["text_primary"]};
            border: 1px solid {c["border"]};
            selection-background-color: {selection_bg};
            selection-color: {selection_text};
            outline: 0;
        }}

        /* ── Tab widget ───────────────────────────────────────────── */

        QTabWidget::pane {{
            background-color: {c["bg_darkest"]};
            border: none;
            border-top: 2px solid {c["primary"]};
        }}

        QTabBar {{
            background-color: {c["bg_surface"]};
        }}

        QTabBar::tab {{
            background-color: {c["bg_surface"]};
            color: {c["text_secondary"]};
            padding: 12px 28px;
            border: none;
            border-bottom: 2px solid transparent;
            font-weight: 500;
            font-size: 10.5pt;
            min-width: 100px;
        }}

        QTabBar::tab:hover {{
            color: {c["text_primary"]};
            background-color: {c["bg_elevated"]};
        }}

        QTabBar::tab:selected {{
            background-color: {selection_bg};
            color: {selection_text};
            border-bottom: 2px solid {selection_border};
        }}

        /* ── Scroll area ──────────────────────────────────────────── */

        QScrollArea {{
            background-color: {c["bg_darkest"]};
            border: none;
        }}

        QScrollArea > QWidget > QWidget {{
            background-color: {c["bg_darkest"]};
        }}

        /* ── Labels ───────────────────────────────────────────────── */

        QLabel {{
            color: {c["text_primary"]};
            background-color: transparent;
        }}

        /* ── Group box ────────────────────────────────────────────── */

        QGroupBox {{
            border: 1px solid {c["border"]};
            border-radius: 8px;
            margin-top: 16px;
            padding-top: 16px;
            color: {c["text_secondary"]};
            font-weight: 600;
        }}

        QGroupBox::title {{
            subcontrol-origin: margin;
            subcontrol-position: top left;
            padding: 0 8px;
            color: {c["text_secondary"]};
        }}

        /* ── Check box ────────────────────────────────────────────── */

        QCheckBox {{
            color: {c["text_primary"]};
            spacing: 8px;
            border: 1px solid transparent;
            border-radius: 4px;
            padding: 2px;
        }}

        QCheckBox:focus {{
            border-color: {c["focus_ring"]};
        }}

        QCheckBox::indicator {{
            width: 16px;
            height: 16px;
            border: 1px solid {c["text_tertiary"]};
            border-radius: 4px;
            background-color: {c["bg_elevated"]};
        }}

        QCheckBox::indicator:checked {{
            background-color: {c["primary"]};
            border-color: {c["primary"]};
        }}

        QCheckBox::indicator:hover {{
            border-color: {c["primary"]};
        }}

        QCheckBox::indicator:focus {{
            border: 2px solid {c["focus_ring"]};
        }}

        /* ── Scrollbar ────────────────────────────────────────────── */

        QScrollBar:vertical {{
            background-color: {c["bg_surface"]};
            width: 10px;
            margin: 0;
            border: none;
            border-radius: 5px;
        }}

        QScrollBar::handle:vertical {{
            background-color: {c["bg_elevated"]};
            min-height: 30px;
            border-radius: 5px;
        }}

        QScrollBar::handle:vertical:hover {{
            background-color: {c["border"]};
        }}

        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical {{
            height: 0;
        }}

        QScrollBar:horizontal {{
            background-color: {c["bg_surface"]};
            height: 10px;
            margin: 0;
            border: none;
            border-radius: 5px;
        }}

        QScrollBar::handle:horizontal {{
            background-color: {c["bg_elevated"]};
            min-width: 30px;
            border-radius: 5px;
        }}

        QScrollBar::handle:horizontal:hover {{
            background-color: {c["border"]};
        }}

        QScrollBar::add-line:horizontal,
        QScrollBar::sub-line:horizontal {{
            width: 0;
        }}

        /* ── Status bar ───────────────────────────────────────────── */

        QStatusBar {{
            background-color: {c["bg_surface"]};
            color: {c["text_secondary"]};
            border-top: 1px solid {c["border_light"]};
            padding: 4px 8px;
            font-size: 9pt;
        }}

        QStatusBar QLabel {{
            color: {c["text_secondary"]};
        }}

        /* ── Menu bar ─────────────────────────────────────────────── */

        QMenuBar {{
            background-color: {c["bg_surface"]};
            color: {c["text_primary"]};
            border-bottom: 1px solid {c["border_light"]};
            padding: 2px;
        }}

        QMenuBar::item {{
            padding: 6px 12px;
            border-radius: 4px;
        }}

        QMenuBar::item:selected {{
            background-color: {c["bg_elevated"]};
        }}

        QMenu {{
            background-color: {c["bg_surface"]};
            color: {c["text_primary"]};
            border: 1px solid {c["border"]};
            border-radius: 8px;
            padding: 4px;
        }}

        QMenu::item {{
            padding: 6px 24px;
            border-radius: 4px;
        }}

        QMenu::item:selected {{
            background-color: {c["bg_elevated"]};
        }}

        QMenu::separator {{
            height: 1px;
            background-color: {c["border_light"]};
            margin: 4px 8px;
        }}

        /* ── Tooltips ─────────────────────────────────────────────── */

        QToolTip {{
            background-color: {c["bg_elevated"]};
            color: {c["text_primary"]};
            border: 1px solid {c["border"]};
            border-radius: 4px;
            padding: 4px 8px;
        }}

        /* ── Splitter ─────────────────────────────────────────────── */

        QSplitter::handle {{
            background-color: {c["border_light"]};
        }}

        QSplitter::handle:horizontal {{
            width: 1px;
        }}

        QSplitter::handle:vertical {{
            height: 1px;
        }}

        /* ── Header view (table headers) ──────────────────────────── */

        QHeaderView::section {{
            background-color: {c["bg_surface"]};
            color: {c["text_secondary"]};
            border: none;
            border-bottom: 1px solid {c["border"]};
            padding: 6px 8px;
            font-weight: 600;
        }}

        /* ── Table / Tree / List views ────────────────────────────── */

        QTableView,
        QTreeView,
        QListView {{
            background-color: {c["bg_darkest"]};
            color: {c["text_primary"]};
            border: 1px solid {c["border"]};
            border-radius: 8px;
            gridline-color: {c["border_light"]};
            selection-background-color: {selection_bg};
            selection-color: {selection_text};
            outline: 0;
        }}

        QTableView::item,
        QTreeView::item,
        QListView::item {{
            padding: 4px;
        }}

        QTableView::item:selected,
        QTreeView::item:selected,
        QListView::item:selected {{
            background-color: {selection_bg};
            color: {selection_text};
            border: 1px solid {selection_border};
        }}

        /* ── List widget ─────────────────────────────────────────── */

        QListWidget {{
            background-color: {c["bg_darkest"]};
            color: {c["text_primary"]};
            border: 1px solid {c["border"]};
            border-radius: 8px;
            outline: 0;
        }}

        QListWidget::item {{
            padding: 6px;
        }}

        QListWidget::item:selected {{
            background-color: {selection_bg};
            color: {selection_text};
        }}

        QListWidget::item:hover {{
            background-color: {c["bg_hover"]};
        }}
    """
