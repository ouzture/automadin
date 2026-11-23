from __future__ import annotations

import ast
import re
from pathlib import Path

from automadin.ui.theme import (
    CATEGORY_COLORS,
    COLORS,
    DEFAULT_THEME_NAME,
    STRIP_COLORS,
    available_themes,
    get_stylesheet,
    set_active_theme,
)


EXPECTED_THEMES = ["Midnight Neon", "Solar Paper"]
REQUIRED_COLOR_TOKENS = {
    "bg_darkest",
    "bg_surface",
    "bg_elevated",
    "bg_card",
    "bg_hover",
    "input_bg",
    "input_disabled",
    "border",
    "border_light",
    "text_primary",
    "text_secondary",
    "text_tertiary",
    "text_disabled",
    "text_on_accent",
    "primary_text",
    "success_text",
    "warning_text",
    "error_text",
    "primary",
    "primary_hover",
    "success",
    "success_hover",
    "warning",
    "warning_hover",
    "error",
    "error_hover",
    "purple",
    "pink",
    "orange",
    "green",
    "yellow",
    "blue",
    "red",
    "cyan",
    "accent",
    "focus_ring",
    "selection_bg",
    "selection_border",
    "selection_text",
    "canvas_dark",
    "overlay_click",
    "overlay_match",
    "overlay_best",
    "status_success",
    "status_warning",
    "status_error",
    "status_optional",
}
REQUIRED_CATEGORIES = {
    "ORGANIZATION",
    "ACTIONS",
    "DETECTION",
    "CONTROL_FLOW",
    "TIMING",
    "ANDROID",
    "BROWSER",
    "WINDOWS",
}
COLOR_LITERAL = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|rgba?\s*\(|QColor\s*\(\s*[\"']"
    r"(?:white|black|red|green|blue|yellow|orange)[\"']",
    re.IGNORECASE,
)


def _relative_luminance(color: str) -> float:
    components = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        component / 12.92
        if component <= 0.04045
        else ((component + 0.055) / 1.055) ** 2.4
        for component in components
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def test_only_two_distinct_themes_are_available() -> None:
    assert available_themes() == EXPECTED_THEMES


def test_every_theme_defines_the_complete_visual_vocabulary() -> None:
    try:
        for theme_name in available_themes():
            set_active_theme(theme_name)
            assert set(COLORS) == REQUIRED_COLOR_TOKENS
            assert set(CATEGORY_COLORS) == REQUIRED_CATEGORIES
            assert len(STRIP_COLORS) == 8
    finally:
        set_active_theme(DEFAULT_THEME_NAME)


def test_themes_have_dramatically_different_surfaces_and_accents() -> None:
    try:
        set_active_theme("Midnight Neon")
        midnight_background = COLORS["bg_darkest"]
        midnight_primary = COLORS["primary"]
        midnight_categories = dict(CATEGORY_COLORS)

        set_active_theme("Solar Paper")
        solar_background = COLORS["bg_darkest"]

        assert _relative_luminance(midnight_background) < 0.02
        assert _relative_luminance(solar_background) > 0.85
        assert COLORS["primary"] != midnight_primary
        assert CATEGORY_COLORS != midnight_categories
    finally:
        set_active_theme(DEFAULT_THEME_NAME)


def test_unknown_or_retired_theme_falls_back_to_default() -> None:
    try:
        assert set_active_theme("Harbor Green") == DEFAULT_THEME_NAME
    finally:
        set_active_theme(DEFAULT_THEME_NAME)


def test_ui_colors_are_declared_only_in_theme_module() -> None:
    ui_root = Path(__file__).parents[1] / "automadin" / "ui"
    violations: list[str] = []

    for source_path in sorted(ui_root.rglob("*.py")):
        if source_path.name == "theme.py":
            continue
        for line_number, line in enumerate(
            source_path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if COLOR_LITERAL.search(line):
                violations.append(f"{source_path.relative_to(ui_root)}:{line_number}")

    assert violations == []


def test_ui_modules_do_not_cache_active_theme_colors_at_import_time() -> None:
    ui_root = Path(__file__).parents[1] / "automadin" / "ui"
    violations: list[str] = []

    for source_path in sorted(ui_root.rglob("*.py")):
        if source_path.name == "theme.py":
            continue
        module = ast.parse(source_path.read_text(encoding="utf-8"))
        for statement in module.body:
            if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id in {"COLORS", "CATEGORY_COLORS"}
                for node in ast.walk(statement)
            ):
                violations.append(
                    f"{source_path.relative_to(ui_root)}:{statement.lineno}"
                )

    assert violations == []


def test_native_control_arrows_are_not_replaced_with_css_border_shapes() -> None:
    stylesheet = get_stylesheet()

    assert "QComboBox::down-arrow" not in stylesheet
    assert "QComboBox::drop-down" not in stylesheet
    assert "QSpinBox::up-arrow" not in stylesheet
    assert "QSpinBox::down-arrow" not in stylesheet
    assert "QSpinBox::up-button" not in stylesheet
    assert "QSpinBox::down-button" not in stylesheet
