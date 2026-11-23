from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from automadin import app, diagnostics
from automadin.instance_lock import DataDirectoryInUseError, DataDirectoryLock, LockOwner


@pytest.fixture(autouse=True)
def _restore_diagnostics_state():
    previous_hook = sys.excepthook
    yield
    sys.excepthook = previous_hook
    diagnostics.shutdown_logging()


def test_logging_is_durable_and_rotates_without_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "stderr", None)

    log_path = diagnostics.configure_logging(
        tmp_path,
        max_bytes=300,
        backup_count=2,
    )
    logger = logging.getLogger("automadin.rotation-test")
    for index in range(20):
        logger.info("rotation-record-%02d %s", index, "x" * 120)
    diagnostics.shutdown_logging()

    assert log_path == tmp_path / "logs" / "automadin.log"
    assert log_path.is_file()
    assert log_path.with_name("automadin.log.1").is_file()
    combined = "".join(
        path.read_text(encoding="utf-8") for path in log_path.parent.glob("automadin.log*")
    )
    assert "rotation-record" in combined


def test_uncaught_exception_hook_logs_traceback_and_reports_log_path(tmp_path: Path) -> None:
    log_path = diagnostics.configure_logging(tmp_path)
    dialogs: list[tuple[BaseException, Path | None]] = []
    diagnostics.install_exception_hook(log_path, dialog=lambda error, path: dialogs.append((error, path)))

    try:
        raise ValueError("callback failed")
    except ValueError as error:
        sys.excepthook(type(error), error, error.__traceback__)

    assert len(dialogs) == 1
    assert str(dialogs[0][0]) == "callback failed"
    assert dialogs[0][1] == log_path
    contents = log_path.read_text(encoding="utf-8")
    assert "Unhandled application exception" in contents
    assert "ValueError: callback failed" in contents


def test_main_reports_startup_failure_from_no_console_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected_log_path = tmp_path / "logs" / "automadin.log"
    reported: list[tuple[BaseException, Path | None]] = []
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(app, "_resolve_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(app, "_debug_logging_requested", lambda: False)
    monkeypatch.setattr(
        app,
        "_run_application",
        lambda _data_dir, _log_path, **_kwargs: (
            _ for _ in ()
        ).throw(RuntimeError("database failed")),
    )
    monkeypatch.setattr(
        app,
        "show_startup_error",
        lambda error, path: reported.append((error, path)),
    )

    with pytest.raises(SystemExit) as result:
        app.main()

    assert result.value.code == 1
    assert len(reported) == 1
    assert str(reported[0][0]) == "database failed"
    assert reported[0][1] == expected_log_path
    contents = expected_log_path.read_text(encoding="utf-8")
    assert "Automadin startup failed" in contents
    assert "RuntimeError: database failed" in contents


def test_main_preserves_normal_application_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(app, "_resolve_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(app, "_debug_logging_requested", lambda: False)
    monkeypatch.setattr(
        app,
        "_run_application",
        lambda _data_dir, _log_path, **_kwargs: 23,
    )
    monkeypatch.setattr(
        app,
        "show_startup_error",
        lambda *_args: pytest.fail("normal exit must not show a startup error"),
    )

    with pytest.raises(SystemExit) as result:
        app.main()

    assert result.value.code == 23


def test_startup_error_message_includes_resolved_log_location(tmp_path: Path) -> None:
    log_path = diagnostics.diagnostics_log_path(tmp_path)

    message = diagnostics.startup_error_message(OSError("access denied"), log_path)

    assert "Automadin could not start" in message
    assert "access denied" in message
    assert str(log_path) in message


def test_startup_error_uses_qt_dialog_when_no_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    displayed: list[tuple[str, str]] = []
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(
        diagnostics,
        "_show_qt_error_dialog",
        lambda title, message: displayed.append((title, message)),
    )
    log_path = diagnostics.diagnostics_log_path(tmp_path)

    diagnostics.show_startup_error(RuntimeError("frozen startup failed"), log_path)

    assert len(displayed) == 1
    assert displayed[0][0] == "Automadin Could Not Start"
    assert str(log_path) in displayed[0][1]


def test_data_directory_lock_dialog_works_without_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    displayed: list[tuple[str, str]] = []
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(
        diagnostics,
        "_show_qt_error_dialog",
        lambda title, message: displayed.append((title, message)),
    )

    diagnostics.show_data_directory_in_use(tmp_path, pid=4321, application="Automadin.exe")

    assert displayed[0][0] == "Automadin Is Already Running"
    assert str(tmp_path) in displayed[0][1]
    assert "PID 4321" in displayed[0][1]


def test_main_acquires_directory_lock_before_logging_or_application_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []

    class FakeLock:
        def __init__(self, _data_dir: str) -> None:
            events.append("lock-created")

        def acquire(self) -> None:
            events.append("lock-acquired")

        def release(self) -> None:
            events.append("lock-released")

    monkeypatch.setattr(app, "_resolve_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(app, "_debug_logging_requested", lambda: False)
    monkeypatch.setattr(app, "DataDirectoryLock", FakeLock)
    monkeypatch.setattr(
        app,
        "configure_logging",
        lambda *_args, **_kwargs: events.append("logging-configured"),
    )
    monkeypatch.setattr(
        app,
        "_run_application",
        lambda *_args, **_kwargs: events.append("application-started") or 0,
    )

    with pytest.raises(SystemExit) as result:
        app.main()

    assert result.value.code == 0
    assert events == [
        "lock-created",
        "lock-acquired",
        "logging-configured",
        "application-started",
        "lock-released",
    ]


def test_main_rejects_same_directory_before_logging_or_runtime_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = LockOwner(pid=9876, hostname="host", application="Automadin.exe")
    error = DataDirectoryInUseError(tmp_path, tmp_path / ".automadin.lock", owner)
    displayed: list[tuple[Path, int, str]] = []

    class BusyLock:
        def __init__(self, _data_dir: str) -> None:
            pass

        def acquire(self) -> None:
            raise error

        def release(self) -> None:
            pass

    monkeypatch.setattr(app, "_resolve_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(app, "DataDirectoryLock", BusyLock)
    monkeypatch.setattr(
        app,
        "_try_activate_existing_instance",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        app,
        "configure_logging",
        lambda *_args, **_kwargs: pytest.fail("logging must not open the shared file"),
    )
    monkeypatch.setattr(
        app,
        "_run_application",
        lambda *_args, **_kwargs: pytest.fail(
            "runtime/database startup must not begin"
        ),
    )
    monkeypatch.setattr(
        app,
        "show_data_directory_in_use",
        lambda path, *, pid, application: displayed.append((path, pid, application)),
    )

    with pytest.raises(SystemExit) as result:
        app.main()

    assert result.value.code == 2
    assert displayed == [(tmp_path, 9876, "Automadin.exe")]


def test_busy_second_launch_activates_existing_window_without_shared_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner = LockOwner(pid=9876, hostname="host", application="Automadin.exe")
    error = DataDirectoryInUseError(tmp_path, tmp_path / ".automadin.lock", owner)
    activation_requests: list[tuple[Path, int]] = []
    releases: list[bool] = []

    class BusyLock:
        def __init__(self, _data_dir: str) -> None:
            pass

        def acquire(self) -> None:
            raise error

        def release(self) -> None:
            releases.append(True)

    monkeypatch.setattr(app, "_resolve_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(app, "_runtime_manager_requested", lambda: False)
    monkeypatch.setattr(app, "DataDirectoryLock", BusyLock)
    monkeypatch.setattr(
        app,
        "_try_activate_existing_instance",
        lambda path, *, owner_pid, timeout_ms: activation_requests.append(
            (path, owner_pid)
        )
        or True,
    )
    monkeypatch.setattr(
        app,
        "configure_logging",
        lambda *_args, **_kwargs: pytest.fail(
            "activated launch must not open the shared log"
        ),
    )
    monkeypatch.setattr(
        app,
        "_run_application",
        lambda *_args, **_kwargs: pytest.fail(
            "activated launch must not start runtime or database services"
        ),
    )
    monkeypatch.setattr(
        app,
        "show_data_directory_in_use",
        lambda *_args, **_kwargs: pytest.fail(
            "acknowledged activation must not show an error dialog"
        ),
    )

    with pytest.raises(SystemExit) as result:
        app.main()

    assert result.value.code == 0
    assert activation_requests == [(tmp_path, 9876)]
    assert releases == [True]


def test_failed_activation_retries_lock_when_primary_exits_during_handshake(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner = LockOwner(pid=9876, hostname="host", application="Automadin.exe")
    error = DataDirectoryInUseError(tmp_path, tmp_path / ".automadin.lock", owner)
    attempts = 0
    launched: list[bool] = []

    class HandoffLock:
        def __init__(self, _data_dir: str) -> None:
            self.acquired = False

        def acquire(self) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise error
            self.acquired = True

        def release(self) -> None:
            self.acquired = False

    monkeypatch.setattr(app, "_resolve_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(app, "_runtime_manager_requested", lambda: False)
    monkeypatch.setattr(app, "_debug_logging_requested", lambda: False)
    monkeypatch.setattr(app, "DataDirectoryLock", HandoffLock)
    monkeypatch.setattr(
        app,
        "_try_activate_existing_instance",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        app,
        "_run_application",
        lambda *_args, **_kwargs: launched.append(True) or 0,
    )

    with pytest.raises(SystemExit) as result:
        app.main()

    assert result.value.code == 0
    assert attempts == 2
    assert launched == [True]


def test_activation_retries_a_replacement_lock_owner_within_one_handoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_owner = LockOwner(pid=1111, hostname="host", application="Automadin.exe")
    second_owner = LockOwner(pid=2222, hostname="host", application="Automadin.exe")
    errors = [
        DataDirectoryInUseError(tmp_path, tmp_path / ".automadin.lock", first_owner),
        DataDirectoryInUseError(tmp_path, tmp_path / ".automadin.lock", second_owner),
    ]
    lock_attempts = 0
    activation_requests: list[tuple[int, int]] = []

    class ChangingOwnerLock:
        def __init__(self, _data_dir: str) -> None:
            pass

        def acquire(self) -> None:
            nonlocal lock_attempts
            error = errors[min(lock_attempts, len(errors) - 1)]
            lock_attempts += 1
            raise error

        def release(self) -> None:
            pass

    def activate(_path, *, owner_pid: int, timeout_ms: int) -> bool:
        activation_requests.append((owner_pid, timeout_ms))
        return owner_pid == second_owner.pid

    monkeypatch.setattr(app, "_resolve_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(app, "_runtime_manager_requested", lambda: False)
    monkeypatch.setattr(app, "DataDirectoryLock", ChangingOwnerLock)
    monkeypatch.setattr(app, "_try_activate_existing_instance", activate)
    monkeypatch.setattr(
        app,
        "configure_logging",
        lambda *_args, **_kwargs: pytest.fail(
            "an activated secondary must not open the shared log"
        ),
    )
    monkeypatch.setattr(
        app,
        "_run_application",
        lambda *_args, **_kwargs: pytest.fail(
            "an activated secondary must not start application services"
        ),
    )

    with pytest.raises(SystemExit) as result:
        app.main()

    assert result.value.code == 0
    assert [pid for pid, _timeout in activation_requests] == [1111, 2222]
    assert all(0 < timeout <= app.ACTIVATION_OWNER_ATTEMPT_MILLISECONDS for _, timeout in activation_requests)


def test_held_main_lock_blocks_another_normal_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    held_lock = DataDirectoryLock(tmp_path)
    held_lock.acquire()
    displayed: list[Path] = []
    monkeypatch.setattr(app, "_resolve_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(app, "_runtime_manager_requested", lambda: False)
    monkeypatch.setattr(
        app,
        "_try_activate_existing_instance",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        app,
        "configure_logging",
        lambda *_args, **_kwargs: pytest.fail("blocked app must not open the shared log"),
    )
    monkeypatch.setattr(
        app,
        "_run_application",
        lambda *_args, **_kwargs: pytest.fail("blocked app must not start"),
    )
    monkeypatch.setattr(
        app,
        "show_data_directory_in_use",
        lambda path, **_kwargs: displayed.append(path),
    )

    try:
        with pytest.raises(SystemExit) as result:
            app.main()
    finally:
        held_lock.release()

    assert result.value.code == 2
    assert displayed == [held_lock.data_dir]


def test_runtime_manager_skips_main_lock_and_uses_separate_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    main_log = diagnostics.configure_logging(tmp_path)
    logging.getLogger("automadin.test").info("main-only-record")
    diagnostics.shutdown_logging()
    held_lock = DataDirectoryLock(tmp_path)
    held_lock.acquire()
    launched_with: list[tuple[Path, bool]] = []
    monkeypatch.setattr(app, "_resolve_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(app, "_runtime_manager_requested", lambda: True)
    monkeypatch.setattr(app, "_debug_logging_requested", lambda: False)
    monkeypatch.setattr(
        app,
        "DataDirectoryLock",
        lambda *_args: pytest.fail("Runtime Manager must not take the main app lock"),
    )
    monkeypatch.setattr(
        app,
        "_try_activate_existing_instance",
        lambda *_args, **_kwargs: pytest.fail(
            "Runtime Manager must not use main-app activation IPC"
        ),
    )
    monkeypatch.setattr(
        app,
        "_run_application",
        lambda _data_dir,
        log_path,
        *,
        runtime_manager,
        scheduler_host: launched_with.append(
            (log_path, runtime_manager, scheduler_host),
        )
        or 0,
    )

    try:
        with pytest.raises(SystemExit) as result:
            app.main()
    finally:
        held_lock.release()

    helper_log = tmp_path / "logs" / "runtime-manager.log"
    assert result.value.code == 0
    assert launched_with == [(helper_log, True, False)]
    assert helper_log.is_file()
    assert "Starting Runtime Manager" in helper_log.read_text(encoding="utf-8")
    assert "main-only-record" in main_log.read_text(encoding="utf-8")
    assert "Starting Runtime Manager" not in main_log.read_text(encoding="utf-8")
