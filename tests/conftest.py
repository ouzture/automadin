"""Shared pytest fixtures for deterministic Qt UI tests."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from automadin.ui.theme import DEFAULT_THEME_NAME, set_active_theme


_UI_TEST_HINTS = (
    "dialog",
    "editor",
    "launcher",
    "main_window",
    "picker",
    "selector",
    "typography",
    "ui",
)
_RELEASE_TESTS = {
    "test_build_info.py",
    "test_installer_manifest.py",
    "test_installer_smoke.py",
    "test_quality_workflow.py",
    "test_release_environment.py",
    "test_release_metadata.py",
}
_SLOW_TESTS = {
    "test_browser_health.py",
    "test_browser_profile_management.py",
    "test_browser_profile_storage.py",
    "test_portability_integration.py",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Apply stable suite categories without repeating markers in every file."""
    for item in items:
        filename = item.path.name
        stem = item.path.stem
        if any(hint in stem for hint in _UI_TEST_HINTS):
            item.add_marker(pytest.mark.ui)
        if "integration" in stem:
            item.add_marker(pytest.mark.integration)
        if filename in _RELEASE_TESTS:
            item.add_marker(pytest.mark.release)
        if stem.startswith("test_windows") or filename in _RELEASE_TESTS:
            item.add_marker(pytest.mark.windows)
        if filename in _SLOW_TESTS:
            item.add_marker(pytest.mark.slow)
        if "integration" not in stem and filename not in _RELEASE_TESTS:
            item.add_marker(pytest.mark.unit)


def _discard_top_level_widgets(application: QApplication) -> None:
    for widget in application.topLevelWidgets():
        widget.hide()
        widget.deleteLater()
    application.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    application.processEvents()


def _reset_qt_application(application: QApplication) -> None:
    application.setStyleSheet("")
    application.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont))
    application.setPalette(application.style().standardPalette())
    set_active_theme(DEFAULT_THEME_NAME)
    application.processEvents()


@pytest.fixture
def application() -> Iterator[QApplication]:
    """Provide a QApplication with global UI state isolated per test."""
    existing = QApplication.instance()
    application = existing if isinstance(existing, QApplication) else QApplication([])
    _discard_top_level_widgets(application)
    _reset_qt_application(application)
    yield application
    _discard_top_level_widgets(application)
    _reset_qt_application(application)
