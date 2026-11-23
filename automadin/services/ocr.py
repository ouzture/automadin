"""Pluggable OCR engines and the persisted Tesseract configuration.

Flow handlers deliberately depend on :class:`ScreenTextOcrEngine`, rather
than on a subprocess, executable path, or a specific OCR provider.  Tesseract
is the first provider because it can be installed separately by the user.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from automadin.subprocess_utils import HIDDEN_PROCESS_CREATION_FLAGS


TESSERACT_PATH_SETTING = "ocr.tesseract.path"
TESSERACT_LANGUAGES_SETTING = "ocr.tesseract.languages"
DEFAULT_TESSERACT_LANGUAGES = ("eng",)
_LANGUAGE_CODE_PATTERN = re.compile(r"[a-z0-9_]+")


class OcrEngineError(RuntimeError):
    """Raised when the selected OCR engine cannot recognise an image."""


@runtime_checkable
class ScreenTextOcrEngine(Protocol):
    """Recognise text from one already-cropped PNG image."""

    def recognize_png(self, png: bytes) -> str: ...


@dataclass(frozen=True, slots=True)
class OcrRecognitionOptions:
    """Optional recognition hints understood by capable OCR providers."""

    page_segmentation_mode: int = 6
    character_whitelist: str | None = None


@runtime_checkable
class ConfigurableScreenTextOcrEngine(Protocol):
    """An OCR provider that accepts per-command recognition hints."""

    def recognize_png_with_options(
        self,
        png: bytes,
        options: OcrRecognitionOptions,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class TesseractConfiguration:
    executable: str | None
    languages: tuple[str, ...] = DEFAULT_TESSERACT_LANGUAGES


@dataclass(frozen=True, slots=True)
class TesseractStatus:
    path: Path | None
    usable: bool
    version: str | None = None
    languages: tuple[str, ...] = ()
    selected_languages: tuple[str, ...] = DEFAULT_TESSERACT_LANGUAGES
    error: str | None = None
    source: str = "unavailable"


def normalise_tesseract_languages(languages: Iterable[object]) -> tuple[str, ...]:
    result: list[str] = []
    for value in languages:
        language = str(value).strip().casefold()
        if not _LANGUAGE_CODE_PATTERN.fullmatch(language):
            raise ValueError(
                "Tesseract language codes may contain only lowercase letters, "
                "numbers, and underscores."
            )
        if language not in result:
            result.append(language)
    return tuple(["eng", *(language for language in result if language != "eng")])


def load_tesseract_configuration(database) -> TesseractConfiguration:
    """Load the user-selected executable and OCR languages from app settings."""
    executable = database.get_app_setting(TESSERACT_PATH_SETTING, "") or ""
    raw_languages = database.get_app_setting(TESSERACT_LANGUAGES_SETTING, "") or ""
    try:
        parsed = json.loads(raw_languages) if raw_languages else DEFAULT_TESSERACT_LANGUAGES
        if not isinstance(parsed, list):
            raise ValueError
        languages = normalise_tesseract_languages(parsed)
    except (TypeError, ValueError, json.JSONDecodeError):
        languages = DEFAULT_TESSERACT_LANGUAGES
    return TesseractConfiguration(executable.strip() or None, languages)


def save_tesseract_configuration(
    database,
    executable: str | Path | None,
    languages: Iterable[object],
) -> TesseractConfiguration:
    """Persist a tested Tesseract choice and its enabled language data."""
    path = str(executable).strip() if executable is not None else ""
    configuration = TesseractConfiguration(
        path or None,
        normalise_tesseract_languages(languages),
    )
    if configuration.executable:
        database.set_app_setting(TESSERACT_PATH_SETTING, configuration.executable)
    else:
        database.delete_app_setting(TESSERACT_PATH_SETTING)
    database.set_app_setting(
        TESSERACT_LANGUAGES_SETTING,
        json.dumps(list(configuration.languages)),
    )
    return configuration


def _candidate_path(configuration: TesseractConfiguration) -> tuple[Path | None, str]:
    if configuration.executable:
        return Path(configuration.executable).expanduser(), "configured"
    discovered = shutil.which("tesseract")
    if discovered:
        return Path(discovered), "PATH"
    return None, "unavailable"


def _run_tesseract(
    path: Path,
    arguments: list[str],
    *,
    input_bytes: bytes | None = None,
    timeout: float = 10,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            [str(path), *arguments],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
        )
    except FileNotFoundError as exc:
        raise OcrEngineError(f"Tesseract was not found at: {path}") from exc
    except PermissionError as exc:
        raise OcrEngineError(f"Windows denied permission to run Tesseract at: {path}") from exc
    except subprocess.TimeoutExpired as exc:
        raise OcrEngineError("Tesseract did not respond before the timeout.") from exc
    except OSError as exc:
        raise OcrEngineError(f"Tesseract could not start: {exc}") from exc


def inspect_tesseract(
    configuration: TesseractConfiguration,
) -> TesseractStatus:
    """Inspect a configured or PATH-discovered Tesseract without mutating state."""
    path, source = _candidate_path(configuration)
    if path is None:
        return TesseractStatus(
            None,
            False,
            selected_languages=configuration.languages,
            error="Tesseract was not found. Install it, then choose tesseract.exe.",
        )
    if not path.is_file():
        return TesseractStatus(
            path,
            False,
            selected_languages=configuration.languages,
            error=f"Tesseract executable does not exist: {path}",
            source=source,
        )
    try:
        version_result = _run_tesseract(path, ["--version"])
        output = (version_result.stdout + version_result.stderr).decode(
            "utf-8", errors="replace"
        ).strip()
        if version_result.returncode != 0 or not output.casefold().startswith("tesseract"):
            return TesseractStatus(
                path,
                False,
                selected_languages=configuration.languages,
                error=output or "The selected file did not identify itself as Tesseract.",
                source=source,
            )
        languages_result = _run_tesseract(path, ["--list-langs"])
        language_output = languages_result.stdout.decode("utf-8", errors="replace")
        languages = tuple(
            line.strip().casefold()
            for line in language_output.splitlines()
            if line.strip() and not line.casefold().startswith("list of available languages")
        )
        if languages_result.returncode != 0 or not languages:
            detail = languages_result.stderr.decode("utf-8", errors="replace").strip()
            return TesseractStatus(
                path,
                False,
                version=output.splitlines()[0],
                selected_languages=configuration.languages,
                error=detail or "Tesseract could not list its installed language data.",
                source=source,
            )
        missing = tuple(language for language in configuration.languages if language not in languages)
        return TesseractStatus(
            path,
            not missing,
            version=output.splitlines()[0],
            languages=languages,
            selected_languages=configuration.languages,
            error=(
                "Selected Tesseract language data is missing: " + ", ".join(missing)
                if missing
                else None
            ),
            source=source,
        )
    except OcrEngineError as exc:
        return TesseractStatus(
            path,
            False,
            selected_languages=configuration.languages,
            error=str(exc),
            source=source,
        )


class TesseractOcrEngine:
    """OCR engine using a caller-supplied, separately installed Tesseract."""

    def __init__(self, configuration_loader: Callable[[], TesseractConfiguration]) -> None:
        self._configuration_loader = configuration_loader
        self._status_lock = threading.Lock()
        self._cached_configuration: TesseractConfiguration | None = None
        self._cached_status: TesseractStatus | None = None

    def invalidate(self) -> None:
        """Forget a previous readiness check after OCR setup changes."""
        with self._status_lock:
            self._cached_configuration = None
            self._cached_status = None

    def _status_for(self, configuration: TesseractConfiguration) -> TesseractStatus:
        with self._status_lock:
            if self._cached_configuration == configuration and self._cached_status is not None:
                return self._cached_status
            status = inspect_tesseract(configuration)
            self._cached_configuration = configuration
            self._cached_status = status
            return status

    def recognize_png(self, png: bytes) -> str:
        return self.recognize_png_with_options(png, OcrRecognitionOptions())

    def recognize_png_with_options(
        self,
        png: bytes,
        options: OcrRecognitionOptions,
    ) -> str:
        configuration = self._configuration_loader()
        status = self._status_for(configuration)
        if not status.usable or status.path is None:
            raise OcrEngineError(status.error or "Tesseract is not ready.")
        arguments = [
            "stdin",
            "stdout",
            "-l",
            "+".join(configuration.languages),
            "--psm",
            str(options.page_segmentation_mode),
        ]
        if options.character_whitelist:
            arguments.extend(
                ["-c", f"tessedit_char_whitelist={options.character_whitelist}"]
            )
        result = _run_tesseract(
            status.path,
            arguments,
            input_bytes=png,
            timeout=20,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise OcrEngineError(detail or f"Tesseract failed with exit code {result.returncode}.")
        return result.stdout.decode("utf-8", errors="replace")


def configured_tesseract_engine(database) -> TesseractOcrEngine:
    """Create the application-level engine that always reads current settings."""
    return TesseractOcrEngine(lambda: load_tesseract_configuration(database))
