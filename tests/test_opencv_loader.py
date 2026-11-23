from types import ModuleType

from automadin.services import opencv_loader


def test_load_cv2_ignores_the_known_frozen_pyside_source_error(monkeypatch) -> None:
    feature = ModuleType("PySide6.support.feature")

    def inspect_module(_module: ModuleType) -> bool:
        raise AttributeError("'dict' object has no attribute 'endswith'")

    feature._mod_uses_pyside = inspect_module  # type: ignore[attr-defined]
    cv2 = ModuleType("cv2")
    observed: dict[str, bool] = {}

    def import_module(name: str) -> ModuleType:
        if name == "PySide6.support.feature":
            return feature
        if name == "cv2":
            observed["uses_pyside"] = feature._mod_uses_pyside(cv2)  # type: ignore[attr-defined]
            return cv2
        raise AssertionError(f"Unexpected import: {name}")

    monkeypatch.setattr(opencv_loader.importlib, "import_module", import_module)

    assert opencv_loader.load_cv2() is cv2
    assert observed == {"uses_pyside": False}
    assert feature._mod_uses_pyside is inspect_module  # type: ignore[attr-defined]


def test_load_cv2_preserves_unrelated_pyside_inspection_errors(monkeypatch) -> None:
    feature = ModuleType("PySide6.support.feature")

    def inspect_module(_module: ModuleType) -> bool:
        raise AttributeError("unexpected module metadata error")

    feature._mod_uses_pyside = inspect_module  # type: ignore[attr-defined]
    cv2 = ModuleType("cv2")

    def import_module(name: str) -> ModuleType:
        if name == "PySide6.support.feature":
            return feature
        if name == "cv2":
            feature._mod_uses_pyside(cv2)  # type: ignore[attr-defined]
        raise AssertionError(f"Unexpected import: {name}")

    monkeypatch.setattr(opencv_loader.importlib, "import_module", import_module)

    try:
        opencv_loader.load_cv2()
    except AttributeError as exc:
        assert str(exc) == "unexpected module metadata error"
    else:
        raise AssertionError("Unexpected PySide inspection errors must not be hidden")
    assert feature._mod_uses_pyside is inspect_module  # type: ignore[attr-defined]
