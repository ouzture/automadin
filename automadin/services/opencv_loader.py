"""Safe OpenCV import for frozen PySide applications."""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any


_FROZEN_SOURCE_INSPECTION_ERROR = "'dict' object has no attribute 'endswith'"


def load_cv2() -> Any:
    """Import OpenCV without letting PySide inspect frozen helper modules.

    OpenCV dynamically imports optional Python helpers during its bootstrap.
    In a Nuitka standalone build, PySide's optional ``__feature__`` import hook
    can mistake frozen module metadata for a source filename and raise the
    known ``dict.endswith`` error. Those helpers do not use PySide, so treat
    only that frozen-source inspection failure as "not a PySide module" while
    OpenCV is imported. The PySide hook is restored immediately afterwards.
    """

    try:
        feature_module = importlib.import_module("PySide6.support.feature")
    except ImportError:
        return importlib.import_module("cv2")

    original = getattr(feature_module, "_mod_uses_pyside", None)
    if not callable(original):
        return importlib.import_module("cv2")

    def safely_detects_pyside(module: ModuleType) -> bool:
        try:
            return original(module)
        except AttributeError as exc:
            if str(exc) == _FROZEN_SOURCE_INSPECTION_ERROR:
                return False
            raise

    setattr(feature_module, "_mod_uses_pyside", safely_detects_pyside)
    try:
        return importlib.import_module("cv2")
    finally:
        setattr(feature_module, "_mod_uses_pyside", original)
