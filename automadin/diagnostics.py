"""Durable application diagnostics and uncaught-exception reporting."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from types import TracebackType
from typing import Callable

LOG_DIRECTORY_NAME = "logs"
LOG_FILE_NAME = "automadin.log"
RUNTIME_MANAGER_LOG_FILE_NAME = "runtime-manager.log"
DEFAULT_MAX_LOG_BYTES = 2 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 4

_HANDLER_MARKER = "_automadin_diagnostics_handler"
_EXCEPTION_HOOK_MARKER = "_automadin_exception_hook"


def diagnostics_log_path(
    data_dir: str | Path,
    *,
    log_file_name: str = LOG_FILE_NAME,
) -> Path:
    """Return the canonical diagnostics log path for *data_dir*."""
    return Path(data_dir).expanduser().resolve() / LOG_DIRECTORY_NAME / log_file_name


def _close_owned_handlers() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if not getattr(handler, _HANDLER_MARKER, False):
            continue
        root.removeHandler(handler)
        handler.close()


def configure_logging(
    data_dir: str | Path,
    *,
    debug: bool = False,
    log_file_name: str = LOG_FILE_NAME,
    max_bytes: int = DEFAULT_MAX_LOG_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> Path:
    """Configure console and rotating-file logging for one application process.

    File logging is always enabled at INFO level so a no-console frozen build has
    useful startup history.  Terminal logging retains the quieter legacy behavior:
    WARNING normally and DEBUG when ``--debug`` is requested.
    """
    log_path = diagnostics_log_path(data_dir, log_file_name=log_file_name)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    _close_owned_handlers()

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s [%(process)d:%(threadName)s] "
        "[%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    file_handler.setFormatter(formatter)
    setattr(file_handler, _HANDLER_MARKER, True)
    root.addHandler(file_handler)

    # Frozen Windows GUI applications commonly have sys.stderr set to None.
    # A StreamHandler created in that state can fail while handling the original
    # exception, so omit it and rely on the durable file handler.
    if sys.stderr is not None:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setLevel(logging.DEBUG if debug else logging.WARNING)
        stream_handler.setFormatter(formatter)
        setattr(stream_handler, _HANDLER_MARKER, True)
        root.addHandler(stream_handler)

    for noisy in ("urllib3", "PIL", "matplotlib", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    app_logger = logging.getLogger("automadin")
    app_logger.setLevel(logging.DEBUG if debug else logging.INFO)
    app_logger.info("Diagnostics initialized; log=%s", log_path)
    if debug:
        app_logger.debug("Debug logging enabled")
    return log_path


def shutdown_logging() -> None:
    """Flush and close handlers installed by :func:`configure_logging`."""
    _close_owned_handlers()


def log_exception(
    context: str,
    exception_type: type[BaseException],
    exception: BaseException,
    traceback: TracebackType | None,
) -> None:
    """Write an exception and traceback to the configured diagnostics log."""
    logging.getLogger("automadin").critical(
        context,
        exc_info=(exception_type, exception, traceback),
    )
    for handler in logging.getLogger().handlers:
        if getattr(handler, _HANDLER_MARKER, False):
            handler.flush()


def startup_error_message(exception: BaseException, log_path: Path | None) -> str:
    """Build concise, actionable text for a startup failure dialog."""
    reason = str(exception).strip() or exception.__class__.__name__
    if log_path is None:
        location = "The diagnostics log location could not be resolved."
    else:
        location = f"Diagnostics log location:\n{log_path}"
    return (
        "Automadin could not start.\n\n"
        f"Reason: {reason}\n\n"
        f"{location}\n\n"
        "Please include the diagnostics log when asking for support."
    )


def _show_qt_error_dialog(title: str, message: str) -> None:
    """Show a critical Qt dialog, creating an application if startup failed early."""
    from PySide6.QtWidgets import QApplication, QMessageBox

    owned_app = None
    if QApplication.instance() is None:
        owned_app = QApplication(sys.argv)
        owned_app.setApplicationName("Automadin")
    QMessageBox.critical(QApplication.activeWindow(), title, message)


def show_startup_error(exception: BaseException, log_path: Path | None) -> None:
    """Report a fatal startup failure without relying on a terminal."""
    message = startup_error_message(exception, log_path)
    try:
        _show_qt_error_dialog("Automadin Could Not Start", message)
    except Exception:
        # If Qt itself cannot initialize there is no GUI channel left.  Preserve
        # both failures in the file log and use stderr only when one exists.
        logging.getLogger("automadin").exception("Could not display startup error dialog")
        if sys.stderr is not None:
            print(message, file=sys.stderr)


def show_unexpected_error(exception: BaseException, log_path: Path | None) -> None:
    """Report an uncaught in-application error and point to durable details."""
    reason = str(exception).strip() or exception.__class__.__name__
    location = (
        f"Diagnostics log location:\n{log_path}"
        if log_path is not None
        else "The diagnostics log location could not be resolved."
    )
    message = (
        "Automadin encountered an unexpected error.\n\n"
        f"Reason: {reason}\n\n"
        f"{location}\n\n"
        "Please include the diagnostics log when asking for support."
    )
    try:
        _show_qt_error_dialog("Unexpected Automadin Error", message)
    except Exception:
        logging.getLogger("automadin").exception("Could not display unexpected error dialog")
        if sys.stderr is not None:
            print(message, file=sys.stderr)


def show_data_directory_in_use(
    data_dir: Path,
    *,
    pid: int = 0,
    application: str = "",
) -> None:
    """Explain why a second process cannot share one data directory."""
    owner = application.strip() or "another Automadin process"
    if pid > 0:
        owner = f"{owner} (PID {pid})"
    message = (
        "Automadin is already using this data directory.\n\n"
        "To protect your database and browser profiles, this instance will close.\n\n"
        f"Data directory:\n{data_dir}\n\n"
        f"Current owner: {owner}\n\n"
        "Close the other Automadin window and try again. A lock left by a crashed "
        "process is recovered automatically."
    )
    try:
        _show_qt_error_dialog("Automadin Is Already Running", message)
    except Exception:
        logging.getLogger("automadin").exception("Could not display data-directory lock dialog")
        if sys.stderr is not None:
            print(message, file=sys.stderr)


def install_exception_hook(
    log_path: Path,
    *,
    dialog: Callable[[BaseException, Path | None], None] | None = None,
) -> None:
    """Install an idempotent main-thread hook for uncaught Python/Qt exceptions."""
    current_hook = sys.excepthook
    if getattr(current_hook, _EXCEPTION_HOOK_MARKER, False):
        previous_hook = getattr(current_hook, "_automadin_previous_hook", sys.__excepthook__)
    else:
        previous_hook = current_hook
    error_dialog = dialog or show_unexpected_error

    def exception_hook(
        exception_type: type[BaseException],
        exception: BaseException,
        traceback: TracebackType | None,
    ) -> None:
        if issubclass(exception_type, KeyboardInterrupt):
            previous_hook(exception_type, exception, traceback)
            return
        log_exception("Unhandled application exception", exception_type, exception, traceback)
        error_dialog(exception, log_path)

    setattr(exception_hook, _EXCEPTION_HOOK_MARKER, True)
    setattr(exception_hook, "_automadin_previous_hook", previous_hook)
    sys.excepthook = exception_hook
