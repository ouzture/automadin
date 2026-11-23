from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from automadin import app
from automadin.ui import main_window
from automadin.ui.main_window import MainWindow


class _Database:
    def __init__(self, profiles, *, acknowledged: str = "") -> None:
        self.profiles = list(profiles)
        self.settings = (
            {main_window.PROFILE_SETUP_NOTICE_SETTING: acknowledged}
            if acknowledged
            else {}
        )

    def get_app_setting(self, key: str, default=None):
        return self.settings.get(key, default)

    def set_app_setting(self, key: str, value: str) -> None:
        self.settings[key] = value

    def list_profile_records(self):
        return list(self.profiles)


class _StatusBar:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def showMessage(self, message: str, _timeout: int = 0) -> None:
        self.messages.append(message)


class _WindowHarness:
    def __init__(self, database: _Database) -> None:
        self._db = database
        self._status = _StatusBar()
        self.reviewed = False

    def statusBar(self) -> _StatusBar:
        return self._status

    def _show_browser_profile_import_dialog(self) -> None:
        self.reviewed = True


class _MessageBox:
    class Icon:
        Information = object()

    class ButtonRole:
        ActionRole = object()
        RejectRole = object()

    review_clicked = False
    created = 0

    def __init__(self, _parent) -> None:
        type(self).created += 1
        self._review_button = None
        self._clicked = None

    def setIcon(self, _icon) -> None:
        pass

    def setWindowTitle(self, _title: str) -> None:
        pass

    def setText(self, _text: str) -> None:
        pass

    def setInformativeText(self, _text: str) -> None:
        pass

    def addButton(self, text: str, _role):
        button = object()
        if text == "Review Browser Profiles":
            self._review_button = button
        return button

    def exec(self) -> None:
        self._clicked = self._review_button if type(self).review_clicked else None

    def clickedButton(self):
        return self._clicked


def test_notice_is_noop_without_unresolved_profiles_or_after_acknowledgement(
    monkeypatch,
) -> None:
    monkeypatch.setattr(main_window, "QMessageBox", _MessageBox)
    monkeypatch.setattr(
        main_window,
        "_profile_recovery_attention",
        lambda _db: main_window.ProfileRecoveryAttention(),
    )
    _MessageBox.created = 0

    monkeypatch.setattr(main_window, "_profiles_requiring_storage_setup", lambda _db: [])
    empty = _WindowHarness(_Database([]))
    MainWindow.show_profile_setup_notice(empty)  # type: ignore[arg-type]
    profile = SimpleNamespace(profile_id="profile-1", display_label="Work")
    attention = [(profile, "missing")]
    fingerprint = main_window._profile_setup_notice_fingerprint(attention)
    monkeypatch.setattr(
        main_window,
        "_profiles_requiring_storage_setup",
        lambda _db: attention,
    )
    acknowledged = _WindowHarness(
        _Database(
            [profile],
            acknowledged=fingerprint,
        )
    )
    MainWindow.show_profile_setup_notice(acknowledged)  # type: ignore[arg-type]

    assert _MessageBox.created == 0


def test_notice_records_acknowledgement_and_opens_profile_manager(monkeypatch) -> None:
    monkeypatch.setattr(main_window, "QMessageBox", _MessageBox)
    monkeypatch.setattr(
        main_window,
        "_profile_recovery_attention",
        lambda _db: main_window.ProfileRecoveryAttention(),
    )
    _MessageBox.created = 0
    _MessageBox.review_clicked = True
    profiles = [
        SimpleNamespace(
            profile_id="profile-1",
            display_label="Work — Chrome / Personal",
        ),
        SimpleNamespace(
            profile_id="profile-2",
            display_label="Store — Firefox / Default",
        ),
    ]
    attention = [(profiles[0], "missing"), (profiles[1], "needs_setup")]
    monkeypatch.setattr(
        main_window,
        "_profiles_requiring_storage_setup",
        lambda _db: attention,
    )
    database = _Database(profiles)
    window = _WindowHarness(database)

    MainWindow.show_profile_setup_notice(window)  # type: ignore[arg-type]

    assert _MessageBox.created == 1
    assert database.settings[main_window.PROFILE_SETUP_NOTICE_SETTING] == (
        main_window._profile_setup_notice_fingerprint(attention)
    )
    assert window.reviewed


def test_imported_v2_profile_without_local_uuid_storage_requires_setup(
    monkeypatch,
) -> None:
    ready = SimpleNamespace(
        profile_id="ready-id",
        display_label="Ready",
        engine="chromium",
        identity=SimpleNamespace(browser="Chromium"),
        legacy_storage_name="",
    )
    missing = SimpleNamespace(
        profile_id="missing-id",
        display_label="Imported elsewhere",
        engine="firefox",
        identity=SimpleNamespace(browser="Firefox"),
        legacy_storage_name="",
    )

    def inspect(profile_id, _engine, *, legacy_storage_name):
        assert legacy_storage_name == ""
        status = (
            main_window.ProfileStorageStatus.READY
            if profile_id == ready.profile_id
            else main_window.ProfileStorageStatus.MISSING
        )
        return SimpleNamespace(status=status)

    monkeypatch.setattr(main_window, "inspect_profile_storage", inspect)

    needs_setup = main_window._profiles_requiring_storage_setup(
        _Database([ready, missing])  # type: ignore[arg-type]
    )

    assert needs_setup == [(missing, "missing")]


def test_orphaned_delete_notice_is_visible_and_part_of_acknowledgement(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(main_window, "QMessageBox", _MessageBox)
    _MessageBox.created = 0
    _MessageBox.review_clicked = True
    monkeypatch.setattr(
        main_window,
        "_profiles_requiring_storage_setup",
        lambda _db: [],
    )
    pending = SimpleNamespace(
        profile_id="orphan-id",
        engine="chromium",
        tombstone_path=tmp_path / ".deleting-orphan",
    )
    recovery = main_window.ProfileRecoveryAttention(
        pending_deletes=(pending,),
    )
    monkeypatch.setattr(
        main_window,
        "_profile_recovery_attention",
        lambda _db: recovery,
    )
    database = _Database([])
    window = _WindowHarness(database)

    MainWindow.show_profile_setup_notice(window)  # type: ignore[arg-type]

    assert _MessageBox.created == 1
    assert window.reviewed
    assert database.settings[main_window.PROFILE_SETUP_NOTICE_SETTING] == (
        main_window._profile_setup_notice_fingerprint([], recovery)
    )


def test_recovery_inventory_filters_rows_and_preserves_scan_failures(
    monkeypatch,
    tmp_path: Path,
) -> None:
    profile = SimpleNamespace(profile_id="known-id")
    known = SimpleNamespace(
        profile_id="known-id",
        engine="chromium",
        tombstone_path=tmp_path / "known",
    )
    orphan = SimpleNamespace(
        profile_id="orphan-id",
        engine="firefox",
        tombstone_path=tmp_path / "orphan",
    )
    database = _Database([profile])
    monkeypatch.setattr(
        main_window,
        "inspect_pending_profile_deletes",
        lambda: SimpleNamespace(pending=(known, orphan), blocked=()),
    )
    managed_known = SimpleNamespace(profile_id="known-id")
    managed_orphan = SimpleNamespace(profile_id="other-id")
    monkeypatch.setattr(
        main_window,
        "list_managed_profile_storage",
        lambda: [managed_known, managed_orphan],
    )
    monkeypatch.setattr(main_window, "list_profile_staging_remnants", lambda: [])

    attention = main_window._profile_recovery_attention(database)

    assert attention.pending_deletes == (orphan,)
    assert attention.unlinked_storage == (managed_orphan,)
    assert attention.scan_errors == ()

    def fail_scan():
        raise OSError("scan failed")

    monkeypatch.setattr(main_window, "inspect_pending_profile_deletes", fail_scan)
    failed = main_window._profile_recovery_attention(database)
    assert failed.scan_errors == ("scan failed",)
    assert failed.unlinked_storage == (managed_orphan,)


def test_recovery_fingerprint_includes_unlinked_staging_and_blocked_paths(
    tmp_path: Path,
) -> None:
    first = main_window.ProfileRecoveryAttention(
        unlinked_storage=(
            SimpleNamespace(
                profile_id="profile-id",
                engine="chrome",
                path=tmp_path / "managed",
            ),
        ),
        staging_remnants=(
            SimpleNamespace(
                operation=SimpleNamespace(value="importing"),
                profile_id="profile-id",
                path=tmp_path / "staging",
                has_matching_manifest=False,
                layout_ready=False,
            ),
        ),
        blocked_deletes=(
            SimpleNamespace(
                profile_id="profile-id",
                reason=SimpleNamespace(value="multiple_candidates"),
                candidate_paths=(tmp_path / "one", tmp_path / "two"),
            ),
        ),
    )
    changed = main_window.ProfileRecoveryAttention(
        unlinked_storage=first.unlinked_storage,
        staging_remnants=first.staging_remnants,
        blocked_deletes=(
            SimpleNamespace(
                profile_id="profile-id",
                reason=SimpleNamespace(value="multiple_candidates"),
                candidate_paths=(tmp_path / "one", tmp_path / "three"),
            ),
        ),
    )

    assert main_window._profile_setup_notice_fingerprint([], first) != (
        main_window._profile_setup_notice_fingerprint([], changed)
    )


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(applied=True),
        SimpleNamespace(applied=False, database_activated=True),
        SimpleNamespace(applied=False, active_database_matches_import=True),
    ],
    ids=["complete", "portable-db-active", "database-only-active"],
)
def test_active_imported_database_clears_source_installation_acknowledgement(
    result: SimpleNamespace,
) -> None:
    database = _Database([])
    database.settings[main_window.PROFILE_SETUP_NOTICE_SETTING] = "source-hash"

    app._reset_imported_profile_notice(
        database,
        result,
        main_window.PROFILE_SETUP_NOTICE_SETTING,
    )

    assert database.settings[main_window.PROFILE_SETUP_NOTICE_SETTING] == ""
