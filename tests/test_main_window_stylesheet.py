from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import qInstallMessageHandler
from PySide6.QtWidgets import QApplication

from automadin.services.database import DatabaseService
from automadin.ui.main_window import MainWindow
from automadin.ui.theme import COLORS, DEFAULT_THEME_NAME, available_themes, set_active_theme


def _relative_luminance(color: str) -> float:
    components = [
        int(color[index : index + 2], 16) / 255
        for index in (1, 3, 5)
    ]
    linear = [
        component / 12.92
        if component <= 0.04045
        else ((component + 0.055) / 1.055) ** 2.4
        for component in components
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(first: str, second: str) -> float:
    lighter, darker = sorted(
        (_relative_luminance(first), _relative_luminance(second)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


class MainWindowStylesheetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_navigation_stylesheet_has_no_qt_parse_warnings(self) -> None:
        messages: list[str] = []

        def capture(_message_type, _context, message: str) -> None:
            messages.append(message)

        previous_handler = qInstallMessageHandler(capture)
        temp_dir = tempfile.TemporaryDirectory()
        db = DatabaseService(str(Path(temp_dir.name) / "window.db"))
        window = None
        try:
            window = MainWindow(db)
            window.show()
            self.app.processEvents()
            selector_stylesheet = window._device_selector.styleSheet()
            self.assertIn("QWidget#deviceSelectorBar", selector_stylesheet)
            self.assertNotIn("QWidget {", selector_stylesheet)
            self.assertIs(
                window._utility._capture_controller,
                window._capture_controller,
            )
            self.assertIs(
                window._management._capture_controller,
                window._capture_controller,
            )
            self.assertIs(
                window._flow_editor._capture_controller,
                window._capture_controller,
            )
            with (
                patch.object(
                    window._management,
                    "refresh_resource_list",
                ) as refresh_library,
                patch.object(
                    window._utility,
                    "refresh_image_library",
                ) as refresh_vision,
            ):
                window._flow_editor.resource_saved.emit(
                    "image",
                    "Mobile/New Target",
                )
                self.app.processEvents()
                refresh_library.assert_called_once_with("image")
                refresh_vision.assert_called_once_with()
        finally:
            if window is not None:
                window.close()
                window.deleteLater()
            self.app.processEvents()
            db.close()
            temp_dir.cleanup()
            qInstallMessageHandler(previous_handler)

        parse_warnings = [
            message for message in messages if "Could not parse stylesheet" in message
        ]
        self.assertEqual(parse_warnings, [])

    def test_primary_buttons_meet_normal_text_contrast(self) -> None:
        try:
            for theme_name in available_themes():
                set_active_theme(theme_name)
                for semantic in ("primary", "success", "warning", "error"):
                    foreground = COLORS[f"{semantic}_text"]
                    for background_key in (semantic, f"{semantic}_hover"):
                        self.assertGreaterEqual(
                            _contrast_ratio(foreground, COLORS[background_key]),
                            4.5,
                            f"{theme_name}: {background_key}",
                        )
        finally:
            set_active_theme(DEFAULT_THEME_NAME)


if __name__ == "__main__":
    unittest.main()
