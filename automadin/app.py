"""Application entry point for Automadin."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from automadin.diagnostics import (
    LOG_FILE_NAME,
    RUNTIME_MANAGER_LOG_FILE_NAME,
    configure_logging,
    diagnostics_log_path,
    install_exception_hook,
    log_exception,
    show_data_directory_in_use,
    show_startup_error,
)
from automadin.instance_lock import DataDirectoryInUseError, DataDirectoryLock
from automadin.runtime import (
    PORTABLE_MARKER,
    application_dir,
    configure_runtime,
    default_installed_data_dir,
    hold_installer_app_mutex,
    is_frozen,
)

ACTIVATION_HANDOFF_DEADLINE_MILLISECONDS = 10_000
ACTIVATION_OWNER_ATTEMPT_MILLISECONDS = 5000
ACTIVATION_MAX_OWNER_ATTEMPTS = 3
_BROWSER_COMPANION_ORIGIN = "chrome-extension://mngmmchlajnjfakibmioediekahneckp/"


def _resolve_data_dir() -> str:
    """Resolve the data directory.

    Priority:
    1. --data-dir CLI argument
    2. AUTOMADIN_DATA_DIR environment variable
    3. Portable data directory when --portable or portable.flag is present
    4. Per-user LocalAppData directory for installed/frozen builds
    5. Directory containing the main script during development
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--portable", action="store_true", default=False)
    args, _ = parser.parse_known_args()

    if args.data_dir:
        return os.path.abspath(args.data_dir)

    env = os.environ.get("AUTOMADIN_DATA_DIR")
    if env:
        return os.path.abspath(env)

    app_dir = application_dir()
    if args.portable or (app_dir / PORTABLE_MARKER).is_file():
        return str(app_dir / "data")

    if is_frozen():
        return str(default_installed_data_dir())
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def _debug_logging_requested() -> bool:
    """Return whether verbose logging was requested on the command line."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--debug", action="store_true", default=False)
    args, _ = parser.parse_known_args()
    return args.debug


def _runtime_manager_requested() -> bool:
    """Return whether this process is the standalone Runtime Manager helper."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--runtime-manager", action="store_true", default=False)
    args, _ = parser.parse_known_args()
    return args.runtime_manager


def _scheduler_host_requested() -> bool:
    """Return whether Automadin should start hidden as the scheduler host."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--scheduler-host", action="store_true", default=False)
    args, _ = parser.parse_known_args()
    return args.scheduler_host


def _browser_companion_host_requested() -> bool:
    """Recognize explicit development mode and Chromium native-host launches."""
    return "--browser-companion-host" in sys.argv[1:] or any(
        argument == _BROWSER_COMPANION_ORIGIN for argument in sys.argv[1:]
    )


def _browser_health_worker_target() -> str | None:
    """Return the worker target when this is a browser-health child process."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--browser-health-worker", default=None)
    args, _ = parser.parse_known_args()
    return args.browser_health_worker


def _windows_capture_worker_request() -> tuple[str, str] | None:
    """Return the native capture worker arguments, when requested."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--windows-capture-worker", nargs=2, default=None)
    args, _ = parser.parse_known_args()
    if args.windows_capture_worker is None:
        return None
    hwnd, output_path = args.windows_capture_worker
    return hwnd, output_path


def _support_report_worker_request() -> str | None:
    """Return the request path for an isolated support-report child process."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--support-report-worker", default=None)
    args, _ = parser.parse_known_args()
    return args.support_report_worker


def _release_browser_health_self_test_parent() -> str | None:
    """Return the safe temporary parent requested by the packaged release gate."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--release-browser-health-self-test", default=None)
    args, _ = parser.parse_known_args()
    return args.release_browser_health_self_test


def _release_diagnostics_self_test_request() -> tuple[str, str | None] | None:
    """Return the parent and nonce requested by the diagnostics release gate."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--release-diagnostics-self-test", default=None)
    parser.add_argument("--release-self-test-nonce", default=None)
    args, _ = parser.parse_known_args()
    if args.release_diagnostics_self_test is None:
        return None
    return args.release_diagnostics_self_test, args.release_self_test_nonce


def _release_pushover_self_test_requested() -> bool:
    """Return whether the packaged credential-store release gate was requested."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--release-pushover-credential-self-test",
        action="store_true",
        default=False,
    )
    args, _ = parser.parse_known_args()
    return args.release_pushover_credential_self_test


def _try_activate_existing_instance(
    data_dir: str | Path,
    *,
    owner_pid: int,
    timeout_ms: int = ACTIVATION_OWNER_ATTEMPT_MILLISECONDS,
) -> bool:
    """Best-effort activation without opening the shared log or database."""
    try:
        from automadin.instance_activation import request_existing_instance_activation

        return request_existing_instance_activation(
            data_dir,
            expected_pid=owner_pid,
            timeout_ms=timeout_ms,
        )
    except Exception:
        # The data-directory lock remains the authority.  If IPC is missing,
        # damaged, or unavailable, main() retains the existing safe diagnostic.
        return False


def _acquire_or_activate_existing_instance(
    instance_lock: DataDirectoryLock,
) -> bool:
    """Acquire the data lock or activate a current owner within one deadline.

    Ownership can change while a secondary launch is waiting for the activation
    acknowledgement.  Re-checking the authoritative lock lets that launch either
    become the new owner or contact a replacement owner without opening shared
    application state.
    """
    deadline = time.monotonic() + (
        ACTIVATION_HANDOFF_DEADLINE_MILLISECONDS / 1000
    )
    attempted_owner_pids: set[int] = set()
    while True:
        try:
            instance_lock.acquire()
            return False
        except DataDirectoryInUseError as exc:
            owner_pid = exc.owner.pid
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            if (
                owner_pid <= 0
                or owner_pid in attempted_owner_pids
                or len(attempted_owner_pids) >= ACTIVATION_MAX_OWNER_ATTEMPTS
                or remaining_ms <= 0
            ):
                raise
            attempted_owner_pids.add(owner_pid)
            attempt_ms = min(ACTIVATION_OWNER_ATTEMPT_MILLISECONDS, remaining_ms)
            if _try_activate_existing_instance(
                exc.data_dir,
                owner_pid=owner_pid,
                timeout_ms=attempt_ms,
            ):
                return True


def _reset_imported_profile_notice(
    database: Any,
    import_result: object | None,
    setting_key: str,
) -> None:
    """Discard a source-installation acknowledgement after DB activation."""
    if _imported_database_is_active(import_result):
        database.set_app_setting(setting_key, "")


def _reset_setup_acknowledgement_after_import(
    database: Any,
    import_result: object | None,
) -> None:
    """Re-run this installation's readiness checklist after DB activation."""
    if not _imported_database_is_active(import_result):
        return
    from automadin.services.setup_center import SetupCenterService

    SetupCenterService(database).reset_acknowledgement()


def _imported_database_is_active(import_result: object | None) -> bool:
    """Return the explicit database truth state exposed by an import result."""
    if import_result is None:
        return False
    return any(
        bool(getattr(import_result, field, False))
        for field in (
            "applied",
            "database_activated",
            "active_database_matches_import",
        )
    )


def _apply_pending_startup_imports(
    data_dir: str | Path,
    db_path: str | Path,
) -> tuple[object | None, object | None]:
    """Apply one pending restore, giving a portable backup strict precedence.

    The two return values are ``(portable_result, database_only_result)``.
    A portability plan owns both the database and profile transition, so an
    unrelated database-only import must never run in the same startup.
    """
    from automadin.services.browser_profile_paths import managed_profiles_root
    from automadin.services.database_transfer import apply_pending_database_import
    from automadin.services.portability_activation import (
        apply_pending_portability_activation,
    )

    portable_result = apply_pending_portability_activation(
        data_dir,
        profiles_root=managed_profiles_root(),
    )
    if portable_result is not None:
        return portable_result, None
    return None, apply_pending_database_import(db_path)


def _result_paths(result: object) -> tuple[str, ...]:
    """Return unique recovery paths exposed by a startup result."""
    values: list[object] = list(getattr(result, "recovery_paths", ()) or ())
    values.extend(getattr(result, "unresolved_paths", ()) or ())
    values.extend(
        getattr(result, field, None)
        for field in ("backup_path", "rejected_path", "failed_plan_path")
    )
    return tuple(
        dict.fromkeys(str(value) for value in values if value)
    )


def _show_startup_import_result(
    parent: object,
    message_box: Any,
    portable_result: object | None,
    database_import_result: object | None,
) -> None:
    """Show the one-time outcome for the restore that ran during startup."""
    if portable_result is not None:
        if bool(getattr(portable_result, "applied", False)):
            backup_path = getattr(portable_result, "backup_path", None)
            backup_note = (
                f"\n\nPrevious database safety backup:\n{backup_path}"
                if backup_path
                else ""
            )
            cleanup_error = str(getattr(portable_result, "error", None) or "")
            cleanup_scope = getattr(
                portable_result,
                "maintenance_cleanup_scope",
                None,
            )
            cleanup_retry_error = ""
            cleanup_released = False
            if cleanup_scope is not None:
                try:
                    cleanup_released = bool(
                        cleanup_scope.release()
                        or getattr(cleanup_scope, "released", False)
                    )
                except Exception as exc:
                    cleanup_retry_error = str(exc)
            if cleanup_error or cleanup_scope is not None:
                if cleanup_released:
                    cleanup_note = (
                        "Automadin retried the delayed browser-profile maintenance "
                        "cleanup and released it successfully."
                    )
                    title = "Portable Backup Restored; Cleanup Completed"
                else:
                    cleanup_note = (
                        "Browser-profile maintenance ownership could not be confirmed "
                        "released. Restart Automadin before using the restored profiles."
                    )
                    title = "Portable Backup Restored; Restart Required"
                details = "\n\n".join(
                    item for item in (cleanup_error, cleanup_retry_error) if item
                )
                detail_note = f"\n\nDetails: {details}" if details else ""
                message_box.warning(
                    parent,
                    title,
                    "The portable Automadin workspace and imported database are active, "
                    "but startup maintenance cleanup needed attention.\n\n"
                    f"{cleanup_note}{detail_note}{backup_note}",
                )
                return
            message_box.information(
                parent,
                "Portable Backup Restored",
                "The portable Automadin workspace is now active. Included browser "
                "profile data was restored without overwriting unrelated local "
                "profiles.\n\nSome browsers protect sign-in data for the original "
                "Windows account or computer, so you may need to sign in again."
                "\n\nReview restored profiles under Tools > Browser Profiles."
                f"{backup_note}",
            )
            return

        database_activated = bool(
            getattr(portable_result, "database_activated", False)
        )
        recovery_paths = _result_paths(portable_result)
        recovery_note = (
            "\n\nRecovery files kept for review:\n" + "\n".join(recovery_paths)
            if recovery_paths
            else ""
        )
        if database_activated:
            message_box.warning(
                parent,
                "Portable Backup Needs Recovery",
                "The imported Automadin database is active, but the browser profile "
                "restore did not complete. Automadin preserved recoverable files; "
                "review them before retrying or removing anything.\n\n"
                f"{getattr(portable_result, 'error', None) or 'The restore could not be completed.'}"
                f"{recovery_note}",
            )
            return

        message_box.warning(
            parent,
            "Portable Backup Restore Failed",
            "Automadin stopped the restore and preserved all recoverable data. "
            "The imported database did not become active, and recovery files were "
            "not deleted.\n\n"
            f"{getattr(portable_result, 'error', None) or 'The restore could not be completed.'}"
            f"{recovery_note}",
        )
        return

    if database_import_result is None:
        return
    if bool(getattr(database_import_result, "applied", False)):
        backup_path = getattr(database_import_result, "backup_path", None)
        backup_note = (
            f"\n\nPrevious database backup:\n{backup_path}" if backup_path else ""
        )
        message_box.information(
            parent,
            "Database Imported",
            f"The imported Automadin database is now active.{backup_note}",
        )
        return

    recovery_paths = _result_paths(database_import_result)
    recovery_note = (
        "\n\nRecovery files kept for review:\n" + "\n".join(recovery_paths)
        if recovery_paths
        else ""
    )
    if bool(
        getattr(database_import_result, "active_database_matches_import", False)
    ):
        message_box.warning(
            parent,
            "Database Import Needs Recovery",
            "The imported Automadin database is active, but final verification or "
            "cleanup did not complete. Review the preserved recovery files before "
            "retrying or removing anything.\n\n"
            f"{getattr(database_import_result, 'error', None) or 'The import could not be completed.'}"
            f"{recovery_note}",
        )
        return

    message_box.warning(
        parent,
        "Database Import Failed",
        "The imported database did not become active.\n\n"
        f"{getattr(database_import_result, 'error', None) or 'The import could not be completed.'}"
        f"{recovery_note}",
    )


def _run_application(
    data_dir: str,
    log_path: Path,
    *,
    runtime_manager: bool,
    scheduler_host: bool = False,
) -> int:
    """Initialize and run the Qt application, returning its process exit code."""
    # Keep heavyweight UI/service imports inside the startup boundary.  Import-time
    # failures are then logged and surfaced instead of disappearing in a frozen
    # no-console build.
    from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from automadin.ui.theme import DEFAULT_THEME_NAME, get_stylesheet, set_active_theme
    from automadin.ui.widgets.combo_wheel_guard import install_combo_box_behavior

    os.makedirs(data_dir, exist_ok=True)
    os.environ["AUTOMADIN_DATA_DIR"] = data_dir

    app = QApplication(sys.argv)
    app.setApplicationName("Automadin")
    combo_box_behavior = install_combo_box_behavior(app)
    install_exception_hook(log_path)

    activation_router = None
    activation_server = None
    if not runtime_manager:
        from automadin.instance_activation import InstanceActivationServer

        candidate_server = InstanceActivationServer(data_dir)
        activation_router = candidate_server.router
        try:
            candidate_server.start()
        except Exception:
            logging.getLogger(__name__).exception(
                "Existing-window activation channel could not start; "
                "subsequent launches will use the safe in-use diagnostic."
            )
        else:
            activation_server = candidate_server

    try:
        configure_runtime(data_dir)

        if runtime_manager:
            set_active_theme(DEFAULT_THEME_NAME)
            app.setStyleSheet(get_stylesheet())
            from automadin.ui.runtime_manager import RuntimeManagerDialog

            manager = RuntimeManagerDialog(data_dir)
            manager.show()
            exit_code = app.exec()
            if isinstance(manager, QObject) and QCoreApplication.instance() is app:
                manager.deleteLater()
                QCoreApplication.sendPostedEvents(manager, QEvent.Type.DeferredDelete)
            if (
                isinstance(combo_box_behavior, QObject)
                and QCoreApplication.instance() is app
            ):
                app.removeEventFilter(combo_box_behavior)
                combo_box_behavior.deleteLater()
                QCoreApplication.sendPostedEvents(
                    combo_box_behavior,
                    QEvent.Type.DeferredDelete,
                )
            return exit_code

        from automadin.services.database import DatabaseService
        from automadin.ui.main_window import MainWindow, PROFILE_SETUP_NOTICE_SETTING

        db_path = os.path.join(data_dir, "automadin.db")
        profiles_dir = os.path.join(data_dir, "profiles")
        os.makedirs(profiles_dir, exist_ok=True)
        os.environ["AUTOMADIN_PROFILES_DIR"] = profiles_dir

        portable_result, database_import_result = _apply_pending_startup_imports(
            data_dir,
            db_path,
        )
        import_result = portable_result or database_import_result
        db = DatabaseService(db_path)
        # Notice acknowledgements describe storage on the source installation.
        # Re-evaluate against this machine even when the imported UUID/status set
        # happens to be identical.
        _reset_imported_profile_notice(
            db,
            import_result,
            PROFILE_SETUP_NOTICE_SETTING,
        )
        _reset_setup_acknowledgement_after_import(db, import_result)
        selected_theme = db.get_app_setting("theme", DEFAULT_THEME_NAME)
        set_active_theme(selected_theme)

        app.setStyleSheet(get_stylesheet())

        window = MainWindow(db=db)
        commit_data_request = getattr(app, "commitDataRequest", None)
        if commit_data_request is not None:
            commit_data_request.connect(
                lambda _manager: window.prepare_for_system_shutdown(),
                Qt.ConnectionType.DirectConnection,
            )
        if scheduler_host and window.can_start_hidden():
            if window.has_enabled_schedules():
                window.start_hidden()
            else:
                QTimer.singleShot(0, window.request_exit)
        else:
            window.show()
        if activation_router is not None:
            activation_router.attach_handler(window.activate_from_external_launch)

        _show_startup_import_result(
            window,
            QMessageBox,
            portable_result,
            database_import_result,
        )
        # Schedule guidance only after any synchronous import-result dialog closes.
        # Its nested Qt loop must not race Setup Center/profile guidance.
        if not scheduler_host:
            QTimer.singleShot(250, window.show_startup_guidance)

        exit_code = app.exec()
        if isinstance(window, QObject) and QCoreApplication.instance() is app:
            # Destroy the complete widget tree while QApplication and the Qt
            # platform integration still exist.  Letting Python tear both down
            # together after the event loop can crash in native Qt cleanup.
            window.deleteLater()
            QCoreApplication.sendPostedEvents(window, QEvent.Type.DeferredDelete)
        if (
            isinstance(combo_box_behavior, QObject)
            and QCoreApplication.instance() is app
        ):
            app.removeEventFilter(combo_box_behavior)
            combo_box_behavior.deleteLater()
            QCoreApplication.sendPostedEvents(
                combo_box_behavior,
                QEvent.Type.DeferredDelete,
            )
        return exit_code
    finally:
        if activation_server is not None:
            activation_server.close()


def main() -> None:
    """Launch Automadin behind a startup exception boundary."""
    # Native messaging launches the same executable while the desktop app owns
    # the installer mutex, so dispatch this helper before taking that mutex.
    if _browser_companion_host_requested():
        from automadin.browser_companion_host import run_native_host

        raise SystemExit(run_native_host())

    # Keep the handle for the full lifetime of every installed executable mode.
    # Inno Setup checks the matching AppMutex before touching upgrade files.
    hold_installer_app_mutex()

    windows_capture_request = _windows_capture_worker_request()
    if windows_capture_request is not None:
        from automadin.windows_capture_worker import main as windows_capture_worker_main

        raise SystemExit(windows_capture_worker_main(list(windows_capture_request)))

    support_report_request = _support_report_worker_request()
    if support_report_request is not None:
        from automadin.support_report_worker import main as support_report_worker_main

        raise SystemExit(support_report_worker_main([support_report_request]))

    if _release_pushover_self_test_requested():
        from automadin.pushover_self_test import (
            run_release_pushover_credential_self_test,
        )

        raise SystemExit(run_release_pushover_credential_self_test())

    diagnostics_self_test_request = _release_diagnostics_self_test_request()
    if diagnostics_self_test_request is not None:
        from automadin.diagnostics_self_test import (
            run_release_diagnostics_self_test,
        )

        diagnostics_self_test_parent, diagnostics_self_test_nonce = (
            diagnostics_self_test_request
        )
        raise SystemExit(
            run_release_diagnostics_self_test(
                diagnostics_self_test_parent,
                nonce=diagnostics_self_test_nonce,
            )
        )

    release_self_test_parent = _release_browser_health_self_test_parent()
    if release_self_test_parent is not None:
        from automadin.release_self_test import run_release_browser_health_self_test

        raise SystemExit(run_release_browser_health_self_test(release_self_test_parent))

    browser_health_target = _browser_health_worker_target()
    if browser_health_target is not None:
        from automadin.browser_health_worker import main as browser_health_worker_main

        raise SystemExit(browser_health_worker_main([browser_health_target]))

    log_path: Path | None = None
    instance_lock: DataDirectoryLock | None = None
    companion_broker = None
    try:
        data_dir = _resolve_data_dir()
        runtime_manager = _runtime_manager_requested()
        scheduler_host = _scheduler_host_requested()
        log_file_name = (
            RUNTIME_MANAGER_LOG_FILE_NAME if runtime_manager else LOG_FILE_NAME
        )
        log_path = diagnostics_log_path(data_dir, log_file_name=log_file_name)
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        activated_existing = False
        if not runtime_manager:
            instance_lock = DataDirectoryLock(data_dir)
            activated_existing = _acquire_or_activate_existing_instance(instance_lock)
        if activated_existing:
            exit_code = 0
        else:
            if not runtime_manager:
                from automadin.services.browser_companion_broker import (
                    start_browser_companion_broker,
                )

                companion_broker = start_browser_companion_broker()
            configure_logging(
                data_dir,
                debug=_debug_logging_requested(),
                log_file_name=log_file_name,
            )
            logging.getLogger(__name__).info(
                "Starting %s; data_dir=%s",
                "Runtime Manager" if runtime_manager else "Automadin",
                data_dir,
            )
            exit_code = _run_application(
                data_dir,
                log_path,
                runtime_manager=runtime_manager,
                scheduler_host=scheduler_host,
            )
    except DataDirectoryInUseError as exc:
        show_data_directory_in_use(
            exc.data_dir,
            pid=exc.owner.pid,
            application=exc.owner.application,
        )
        exit_code = 2
    except Exception as exc:
        log_exception("Automadin startup failed", type(exc), exc, exc.__traceback__)
        show_startup_error(exc, log_path)
        exit_code = 1
    finally:
        if companion_broker is not None:
            companion_broker.close()
        if instance_lock is not None:
            instance_lock.release()
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
