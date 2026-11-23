from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from automadin.services import ocr
from automadin.services.ocr import (
    OcrRecognitionOptions,
    TesseractConfiguration,
    TesseractOcrEngine,
    inspect_tesseract,
    load_tesseract_configuration,
    save_tesseract_configuration,
)


class _Database:
    def __init__(self) -> None:
        self.settings: dict[str, str] = {}

    def get_app_setting(self, key: str, default: str | None = None) -> str | None:
        return self.settings.get(key, default)

    def set_app_setting(self, key: str, value: str) -> None:
        self.settings[key] = value

    def delete_app_setting(self, key: str) -> None:
        self.settings.pop(key, None)


def test_tesseract_configuration_defaults_to_english_and_persists_extra_languages() -> None:
    database = _Database()

    initial = load_tesseract_configuration(database)
    saved = save_tesseract_configuration(
        database,
        r"C:\Tools\tesseract.exe",
        ["spa", "eng", "deu", "spa"],
    )

    assert initial.languages == ("eng",)
    assert saved.languages == ("eng", "spa", "deu")
    assert load_tesseract_configuration(database) == saved


def test_tesseract_languages_reject_unsafe_codes() -> None:
    with pytest.raises(ValueError, match="lowercase letters"):
        save_tesseract_configuration(_Database(), None, ["eng", "../bad"])


def test_inspection_reports_missing_selected_language(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    executable = tmp_path / "tesseract.exe"
    executable.write_bytes(b"stub")

    def run(_path, arguments, **_kwargs):
        if arguments == ["--version"]:
            return SimpleNamespace(returncode=0, stdout=b"tesseract 5.5.0\n", stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"List of available languages (1):\neng\n", stderr=b"")

    monkeypatch.setattr(ocr, "_run_tesseract", run)

    status = inspect_tesseract(TesseractConfiguration(str(executable), ("eng", "spa")))

    assert not status.usable
    assert status.languages == ("eng",)
    assert status.error == "Selected Tesseract language data is missing: spa"


def test_tesseract_engine_uses_only_the_interface_and_selected_languages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "tesseract.exe"
    executable.write_bytes(b"stub")
    calls: list[tuple[list[str], bytes | None]] = []

    def run(_path, arguments, **kwargs):
        calls.append((arguments, kwargs.get("input_bytes")))
        if arguments == ["--version"]:
            return SimpleNamespace(returncode=0, stdout=b"tesseract 5.5.0\n", stderr=b"")
        if arguments == ["--list-langs"]:
            return SimpleNamespace(returncode=0, stdout=b"List of available languages (2):\neng\nspa\n", stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"hola\n", stderr=b"")

    monkeypatch.setattr(ocr, "_run_tesseract", run)
    engine = TesseractOcrEngine(
        lambda: TesseractConfiguration(str(executable), ("eng", "spa"))
    )

    assert engine.recognize_png(b"cropped-png") == "hola\n"
    assert calls[-1] == (["stdin", "stdout", "-l", "eng+spa", "--psm", "6"], b"cropped-png")
    assert engine.recognize_png(b"another-cropped-png") == "hola\n"
    assert [arguments for arguments, _input in calls].count(["--version"]) == 1


def test_tesseract_engine_applies_single_value_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "tesseract.exe"
    executable.write_bytes(b"stub")
    calls: list[list[str]] = []

    def run(_path, arguments, **_kwargs):
        calls.append(arguments)
        if arguments == ["--version"]:
            return SimpleNamespace(returncode=0, stdout=b"tesseract 5.5.0\n", stderr=b"")
        if arguments == ["--list-langs"]:
            return SimpleNamespace(returncode=0, stdout=b"eng\n", stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"0:00\n", stderr=b"")

    monkeypatch.setattr(ocr, "_run_tesseract", run)
    engine = TesseractOcrEngine(
        lambda: TesseractConfiguration(str(executable), ("eng",))
    )

    assert engine.recognize_png_with_options(
        b"crop",
        OcrRecognitionOptions(7, "0123456789:"),
    ) == "0:00\n"
    assert calls[-1] == [
        "stdin",
        "stdout",
        "-l",
        "eng",
        "--psm",
        "7",
        "-c",
        "tessedit_char_whitelist=0123456789:",
    ]
