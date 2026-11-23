from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QDialog, QInputDialog, QMessageBox

from automadin.models.device import (
    BrowserDeviceConfig,
    BrowserEngine,
    ScreenMode,
    browser_profile_id_from_saved_device_id,
    browser_profile_name_from_saved_device_id,
)
from automadin.models.run_config import SavedRunConfig
from automadin.services.browser_profile_metadata import BrowserProfileIdentity
from automadin.services.browser_profile_leases import (
    BrowserProfileInUseError,
    BrowserProfileLeaseRegistry,
    ProfileUse,
)
from automadin.services.browser_profile_paths import (
    resolve_profile_storage_path,
)
from automadin.services.browser_profile_storage import (
    ProfileStorageChange,
    ProfileStorageStatus,
    begin_profile_delete,
    create_clean_profile,
    import_browser_profile,
    inspect_profile_storage,
)
from automadin.services.database import DatabaseService
from automadin.ui import browser_profile_import_dialog as profile_dialog
from automadin.ui.browser_profile_import_dialog import (
    BrowserProfileImportDialog,
    _PendingImport,
    _safe_profile_path,
    _stage_browser_profile,
)


def _maintenance(profile_id: str):
    registry = BrowserProfileLeaseRegistry()
    lease = registry.acquire_maintenance(
        profile_id,
        ProfileUse("maintenance", "management test", str(uuid.uuid4())),
    )
    return registry, lease


_raw_create_clean_profile = create_clean_profile
_raw_import_browser_profile = import_browser_profile
_raw_begin_profile_delete = begin_profile_delete


def create_clean_profile(profile_id, *args, **kwargs):
    registry, lease = _maintenance(profile_id)
    kwargs.setdefault("lease_registry", registry)
    kwargs.setdefault("maintenance_lease", lease)
    return _raw_create_clean_profile(profile_id, *args, **kwargs)


def import_browser_profile(source, profile_id, *args, **kwargs):
    registry, lease = _maintenance(profile_id)
    kwargs.setdefault("lease_registry", registry)
    kwargs.setdefault("maintenance_lease", lease)
    return _raw_import_browser_profile(source, profile_id, *args, **kwargs)


def begin_profile_delete(profile_id, *args, **kwargs):
    registry, lease = _maintenance(profile_id)
    kwargs.setdefault("lease_registry", registry)
    kwargs.setdefault("maintenance_lease", lease)
    return _raw_begin_profile_delete(profile_id, *args, **kwargs)


class _LifecycleSignal:
    def __init__(self) -> None:
        self._callbacks = []

    def connect(self, callback) -> None:
        self._callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in tuple(self._callbacks):
            callback(*args)


class _DeferredCopyThread:
    instance = None

    def __init__(
        self,
        _source,
        profile_id,
        _engine,
        **_kwargs,
    ) -> None:
        self.profile_id = profile_id
        self.succeeded = _LifecycleSignal()
        self.failed = _LifecycleSignal()
        self.finished = _LifecycleSignal()
        self.running = False
        self.deleted = 0
        self.wait_calls = 0
        type(self).instance = self

    def start(self) -> None:
        self.running = True

    def isRunning(self) -> bool:
        return self.running

    def wait(self, *_args) -> bool:
        self.wait_calls += 1
        raise AssertionError("Profile dialog must not wait on the GUI thread")

    def deleteLater(self) -> None:
        self.deleted += 1

    def finish(self) -> None:
        self.running = False
        self.finished.emit()


class BrowserProfileCompatibilityTests(unittest.TestCase):
    def test_legacy_path_resolver_remains_explicit_and_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            expected = Path(os.path.abspath(temp_dir)) / "personal"
            self.assertEqual(_safe_profile_path("personal", temp_dir), expected)
            for name in ("../outside", "folder/nested", "..", ""):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    _safe_profile_path(name, temp_dir)

    def test_stage_wrapper_preserves_source_and_uses_launch_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "Google" / "Chrome" / "User Data" / "Default"
            source.mkdir(parents=True)
            (source / "Preferences").write_text('{"profile":{"name":"Work"}}')
            staging = root / "staging"

            _stage_browser_profile(source, staging, "Chrome")

            self.assertTrue((staging / "Default" / "Preferences").is_file())
            self.assertTrue((source / "Preferences").is_file())

    def test_stable_and_legacy_saved_profile_parsers_remain_compatible(self) -> None:
        profile_id = str(uuid.uuid4())
        stable = BrowserDeviceConfig(
            BrowserEngine.CHROMIUM,
            screen_mode=ScreenMode.DESKTOP,
            profile_id=profile_id,
        ).to_id()
        legacy = "chromium|hl|desktop|perm|work-profile-eu"

        self.assertEqual(browser_profile_id_from_saved_device_id(stable), profile_id)
        self.assertEqual(
            browser_profile_name_from_saved_device_id(legacy),
            "work-profile-eu",
        )


class BrowserProfileManagementDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.profiles_root = self.root / "profiles"
        self.profiles_root.mkdir()
        self._environment = patch.dict(
            os.environ,
            {
                "AUTOMADIN_DATA_DIR": str(self.root),
                "AUTOMADIN_PROFILES_DIR": str(self.profiles_root),
                "LOCALAPPDATA": str(self.root / "local"),
                "APPDATA": str(self.root / "roaming"),
            },
        )
        self._environment.start()
        self.database = DatabaseService(str(self.root / "automadin.db"))
        self._dialogs: list[BrowserProfileImportDialog] = []

    def tearDown(self) -> None:
        for dialog in self._dialogs:
            dialog.close()
            dialog.deleteLater()
        QCoreApplication.sendPostedEvents(
            None,
            QEvent.Type.DeferredDelete,
        )
        QApplication.processEvents()
        self.database.close()
        self._environment.stop()
        self._temporary_directory.cleanup()

    def _dialog(
        self,
        lease_registry: BrowserProfileLeaseRegistry | None = None,
    ) -> BrowserProfileImportDialog:
        dialog = BrowserProfileImportDialog(
            self.database,
            lease_registry=lease_registry or BrowserProfileLeaseRegistry(),
        )
        self._dialogs.append(dialog)
        return dialog

    def _legacy_profile(
        self,
        name: str = "Legacy Work",
        *,
        make_folder: bool = True,
    ):
        record = self.database.create_profile(
            name,
            legacy_storage_name=name,
            browser="Chromium",
            source_profile="Default",
            account_name="Jane Doe",
            account_email="jane@example.com",
            engine="chromium",
        )
        if make_folder:
            folder = self.profiles_root / name
            (folder / "Default").mkdir(parents=True)
            (folder / "Default" / "Preferences").write_text("{}", encoding="utf-8")
        return record

    def _ready_profile(self, name: str = "Managed Work"):
        profile_id = str(uuid.uuid4())
        change = create_clean_profile(
            profile_id,
            "chromium",
            profiles_root=self.profiles_root,
        )
        change.commit()
        return self.database.create_profile(
            name,
            profile_id=profile_id,
            browser="Chromium",
            source_profile="Default",
            engine="chromium",
        )

    @staticmethod
    def _select_profile(dialog: BrowserProfileImportDialog, profile_id: str) -> None:
        for row in range(dialog._profiles_table.rowCount()):
            item = dialog._profiles_table.item(row, 0)
            if item.data(Qt.ItemDataRole.UserRole) == profile_id:
                dialog._profiles_table.selectRow(row)
                QApplication.processEvents()
                return
        raise AssertionError(f"Profile {profile_id} was not listed")

    def _source_profile(self) -> Path:
        source = self.root / "Google" / "Chrome" / "User Data" / "Profile 1"
        source.mkdir(parents=True)
        (source / "Preferences").write_text(
            json.dumps(
                {
                    "profile": {"name": "Work"},
                    "account_info": [
                        {"full_name": "Jane Doe", "email": "jane@example.com"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        return source

    def _prepare_copy(
        self,
        dialog: BrowserProfileImportDialog,
        *,
        name: str = "Lifecycle Work",
    ) -> Path:
        source = self._source_profile()
        dialog._source_edit.setText(str(source))
        dialog._identity = BrowserProfileIdentity(
            browser="Chrome",
            source_profile="Profile 1",
            account_name="Jane Doe",
            account_email="jane@example.com",
        )
        dialog._name_edit.setText(name)
        browser_index = dialog._source_browser_combo.findData("Chrome")
        self.assertGreaterEqual(browser_index, 0)
        dialog._source_browser_combo.setCurrentIndex(browser_index)
        return source

    def _orphaned_delete(self):
        record = self._ready_profile("Deleted Portable Profile")
        pending = begin_profile_delete(
            record.profile_id,
            engine=record.engine,
            profiles_root=self.profiles_root,
        )
        self.assertTrue(self.database.delete_profile_by_id(record.profile_id))
        return record, pending

    def _unlinked_storage(self) -> tuple[str, Path]:
        profile_id = str(uuid.uuid4())
        change = create_clean_profile(
            profile_id,
            "chromium",
            profiles_root=self.profiles_root,
        )
        change.commit()
        path = resolve_profile_storage_path(profile_id, self.profiles_root)
        (path / "Default" / "Preferences").write_text(
            json.dumps(
                {
                    "profile": {"name": "Recovered Work"},
                    "account_info": [
                        {
                            "full_name": "Recovered User",
                            "email": "recovered@example.com",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return profile_id, path

    def _staging_remnant(
        self,
        operation: str = "importing",
    ) -> tuple[str, Path]:
        profile_id = str(uuid.uuid4())
        path = self.profiles_root / (
            f".{operation}-{profile_id}-{uuid.uuid4().hex}"
        )
        (path / "Default").mkdir(parents=True)
        (path / "partial-data").write_text("partial", encoding="utf-8")
        return profile_id, path

    def test_refresh_is_read_only_and_uuid_backed_with_clear_needs_setup_status(
        self,
    ) -> None:
        record = self._legacy_profile()
        dialog = self._dialog()
        try:
            with (
                patch.object(self.database, "save_profile") as legacy_save,
                patch.object(self.database, "update_profile_metadata") as update,
                patch.object(self.database, "mark_profile_storage_resolved") as bind,
            ):
                dialog._refresh_imported_profiles(record.profile_id)

            legacy_save.assert_not_called()
            update.assert_not_called()
            bind.assert_not_called()
            self.assertEqual(dialog._profiles_table.rowCount(), 1)
            self.assertEqual(dialog._profiles_table.item(0, 0).text(), "Legacy Work")
            self.assertEqual(dialog._profiles_table.item(0, 1).text(), "Needs setup")
            self.assertEqual(
                dialog._profiles_table.item(0, 0).data(Qt.ItemDataRole.UserRole),
                record.profile_id,
            )
            self.assertTrue(dialog._adopt_profile_btn.isEnabled())
            self.assertTrue(dialog._import_copy_btn.isEnabled())
        finally:
            dialog.close()

    def test_explicit_adoption_moves_legacy_folder_and_binds_uuid(self) -> None:
        record = self._legacy_profile()
        legacy_folder = self.profiles_root / record.legacy_storage_name
        dialog = self._dialog()
        try:
            self._select_profile(dialog, record.profile_id)
            with (
                patch.object(dialog, "_confirm_adoption", return_value=True),
                patch.object(QMessageBox, "information"),
                patch.object(QMessageBox, "warning") as warning,
            ):
                dialog._adopt_selected_profile()

            warning.assert_not_called()
            updated = self.database.get_profile(record.profile_id)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.legacy_storage_name, "")
            self.assertFalse(legacy_folder.exists())
            inspection = inspect_profile_storage(
                record.profile_id,
                "chromium",
                profiles_root=self.profiles_root,
            )
            self.assertEqual(inspection.status, ProfileStorageStatus.READY)
        finally:
            dialog.close()

    def test_adoption_rolls_folder_back_when_database_binding_fails(self) -> None:
        record = self._legacy_profile()
        legacy_folder = self.profiles_root / record.legacy_storage_name
        dialog = self._dialog()
        try:
            self._select_profile(dialog, record.profile_id)
            with (
                patch.object(dialog, "_confirm_adoption", return_value=True),
                patch.object(
                    self.database,
                    "mark_profile_storage_resolved",
                    return_value=False,
                ),
                patch.object(QMessageBox, "warning") as warning,
            ):
                dialog._adopt_selected_profile()

            warning.assert_called_once()
            self.assertTrue(legacy_folder.is_dir())
            self.assertFalse(
                resolve_profile_storage_path(
                    record.profile_id,
                    self.profiles_root,
                ).exists()
            )
            self.assertEqual(
                self.database.get_profile(record.profile_id).legacy_storage_name,
                record.legacy_storage_name,
            )
        finally:
            dialog.close()

    def test_matching_uuid_storage_after_crash_requires_explicit_finish(self) -> None:
        record = self._legacy_profile(make_folder=True)
        change = create_clean_profile(
            record.profile_id,
            "chromium",
            profiles_root=self.profiles_root,
        )
        change.commit()
        legacy_folder = self.profiles_root / record.legacy_storage_name
        dialog = self._dialog()
        try:
            self._select_profile(dialog, record.profile_id)
            self.assertEqual(dialog._adopt_profile_btn.text(), "Finish Setup…")
            self.assertFalse(dialog._import_copy_btn.isEnabled())
            with (
                patch.object(dialog, "_confirm_adoption", return_value=True),
                patch.object(QMessageBox, "information"),
            ):
                dialog._adopt_selected_profile()

            self.assertEqual(
                self.database.get_profile(record.profile_id).legacy_storage_name,
                "",
            )
            self.assertTrue(legacy_folder.is_dir())
            self.assertEqual(
                inspect_profile_storage(
                    record.profile_id,
                    "chromium",
                    profiles_root=self.profiles_root,
                ).status,
                ProfileStorageStatus.READY,
            )
        finally:
            dialog.close()

    def test_crash_published_uuid_storage_cannot_be_database_only_deleted(
        self,
    ) -> None:
        record = self._legacy_profile(make_folder=False)
        change = create_clean_profile(
            record.profile_id,
            "chromium",
            profiles_root=self.profiles_root,
        )
        change.commit()
        managed_path = resolve_profile_storage_path(
            record.profile_id,
            self.profiles_root,
        )
        dialog = self._dialog()
        try:
            self._select_profile(dialog, record.profile_id)
            with (
                patch.object(QMessageBox, "warning") as warning,
                patch.object(dialog, "_confirm_profile_deletion") as confirm,
            ):
                dialog._delete_selected_profile()

            warning.assert_called_once()
            confirm.assert_not_called()
            self.assertIsNotNone(self.database.get_profile(record.profile_id))
            self.assertTrue(managed_path.is_dir())
        finally:
            dialog.close()

    def test_interrupted_delete_can_restore_managed_profile_data(self) -> None:
        record = self._ready_profile()
        managed_path = resolve_profile_storage_path(
            record.profile_id,
            self.profiles_root,
        )
        pending_delete = begin_profile_delete(
            record.profile_id,
            engine=record.engine,
            profiles_root=self.profiles_root,
        )
        self.assertFalse(managed_path.exists())
        self.assertTrue(pending_delete.working_path.is_dir())

        dialog = self._dialog()
        try:
            self._select_profile(dialog, record.profile_id)
            self.assertEqual(
                dialog._adopt_profile_btn.text(),
                "Restore Managed Data…",
            )
            with (
                patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.Yes,
                ),
                patch.object(QMessageBox, "information"),
            ):
                dialog._adopt_selected_profile()

            self.assertTrue(managed_path.is_dir())
            self.assertFalse(pending_delete.working_path.exists())
            self.assertEqual(
                inspect_profile_storage(
                    record.profile_id,
                    record.engine,
                    profiles_root=self.profiles_root,
                ).status,
                ProfileStorageStatus.READY,
            )
        finally:
            dialog.close()

    def test_orphan_delete_refresh_is_read_only_and_shows_exact_details(
        self,
    ) -> None:
        record, pending = self._orphaned_delete()
        with patch(
            "automadin.ui.browser_profile_import_dialog."
            "recover_pending_profile_delete"
        ) as recover:
            dialog = self._dialog()
            try:
                dialog._refresh_imported_profiles()
                recover.assert_not_called()
                self.assertFalse(dialog._orphan_cleanup_panel.isHidden())
                self.assertEqual(dialog._orphan_delete_table.rowCount(), 1)
                self.assertEqual(
                    dialog._orphan_delete_table.item(0, 0).text(),
                    record.profile_id,
                )
                self.assertEqual(
                    dialog._orphan_delete_table.item(0, 1).text(),
                    record.engine,
                )
                self.assertEqual(
                    dialog._orphan_delete_table.item(0, 2).text(),
                    str(pending.working_path),
                )
                self.assertTrue(pending.working_path.is_dir())
            finally:
                dialog.close()

    def test_orphan_delete_keep_action_is_non_mutating(self) -> None:
        _record, pending = self._orphaned_delete()
        dialog = self._dialog()
        try:
            dialog._orphan_delete_table.selectRow(0)
            QApplication.processEvents()
            with (
                patch.object(dialog, "_confirm_orphan_delete", return_value=False),
                patch(
                    "automadin.ui.browser_profile_import_dialog."
                    "recover_pending_profile_delete"
                ) as recover,
            ):
                dialog._finish_selected_orphan_delete()

            recover.assert_not_called()
            self.assertTrue(pending.working_path.is_dir())
            self.assertEqual(dialog._orphan_delete_table.rowCount(), 1)
        finally:
            dialog.close()

    def test_orphan_delete_confirmation_defaults_escape_to_keep(self) -> None:
        record, pending_change = self._orphaned_delete()
        dialog = self._dialog()
        try:
            pending = dialog._orphan_pending_deletes[record.profile_id]
            with patch(
                "automadin.ui.browser_profile_import_dialog.QMessageBox"
            ) as message_type:
                message = message_type.return_value
                finish_button = MagicMock()
                keep_button = MagicMock()
                message.addButton.side_effect = [finish_button, keep_button]
                message.clickedButton.return_value = keep_button

                self.assertFalse(dialog._confirm_orphan_delete(pending))

                message.setDefaultButton.assert_called_once_with(keep_button)
                message.setEscapeButton.assert_called_once_with(keep_button)
                self.assertTrue(pending_change.working_path.is_dir())
        finally:
            dialog.close()

    def test_orphan_delete_can_be_explicitly_finished(self) -> None:
        _record, pending = self._orphaned_delete()
        dialog = self._dialog()
        try:
            dialog._orphan_delete_table.selectRow(0)
            QApplication.processEvents()
            with (
                patch.object(dialog, "_confirm_orphan_delete", return_value=True),
                patch.object(QMessageBox, "information"),
            ):
                dialog._finish_selected_orphan_delete()

            self.assertFalse(pending.working_path.exists())
            self.assertEqual(dialog._orphan_delete_table.rowCount(), 0)
            self.assertTrue(dialog._orphan_cleanup_panel.isHidden())
        finally:
            dialog.close()

    def test_orphan_delete_failure_is_reported_and_keeps_retryable_data(self) -> None:
        _record, pending = self._orphaned_delete()
        dialog = self._dialog()
        try:
            dialog._orphan_delete_table.selectRow(0)
            QApplication.processEvents()
            failed_change = MagicMock()
            failed_change.commit.side_effect = RuntimeError("disk denied")
            with (
                patch.object(dialog, "_confirm_orphan_delete", return_value=True),
                patch(
                    "automadin.ui.browser_profile_import_dialog."
                    "recover_pending_profile_delete",
                    return_value=failed_change,
                ),
                patch.object(QMessageBox, "warning") as warning,
            ):
                dialog._finish_selected_orphan_delete()

            warning.assert_called_once()
            message = str(warning.call_args.args[2])
            self.assertIn("disk denied", message)
            self.assertIn(str(pending.working_path), message)
            self.assertTrue(pending.working_path.is_dir())
            self.assertEqual(dialog._orphan_delete_table.rowCount(), 1)
        finally:
            dialog.close()

    def test_ambiguous_and_malformed_orphan_tombstones_remain_untouched(
        self,
    ) -> None:
        record, pending = self._orphaned_delete()
        duplicate = self.profiles_root / (
            f".deleting-{record.profile_id}-{uuid.uuid4().hex}"
        )
        shutil.copytree(pending.working_path, duplicate)
        malformed_id = str(uuid.uuid4())
        malformed = self.profiles_root / (
            f".deleting-{malformed_id}-{uuid.uuid4().hex}"
        )
        (malformed / "Default").mkdir(parents=True)
        (malformed / "profile.json").write_text("{broken", encoding="utf-8")

        with patch(
            "automadin.ui.browser_profile_import_dialog."
            "recover_pending_profile_delete"
        ) as recover:
            dialog = self._dialog()
            try:
                recover.assert_not_called()
                self.assertEqual(dialog._orphan_delete_table.rowCount(), 2)
                diagnostics = [
                    dialog._orphan_delete_table.item(row, 3).text()
                    for row in range(dialog._orphan_delete_table.rowCount())
                ]
                self.assertTrue(all("Blocked" in text for text in diagnostics))
                dialog._orphan_delete_table.selectRow(0)
                QApplication.processEvents()
                self.assertFalse(dialog._finish_orphan_delete_btn.isEnabled())
                self.assertTrue(pending.working_path.is_dir())
                self.assertTrue(duplicate.is_dir())
                self.assertTrue(malformed.is_dir())
            finally:
                dialog.close()

    def test_recovery_inventory_is_read_only_counted_and_auto_selected(self) -> None:
        profile_id, managed_path = self._unlinked_storage()
        staging_id, staging_path = self._staging_remnant()
        with (
            patch.object(self.database, "create_profile") as create,
            patch(
                "automadin.ui.browser_profile_import_dialog.begin_profile_delete"
            ) as begin_delete,
            patch(
                "automadin.ui.browser_profile_import_dialog."
                "discard_profile_staging_remnant"
            ) as discard,
        ):
            dialog = self._dialog()
            try:
                create.assert_not_called()
                begin_delete.assert_not_called()
                discard.assert_not_called()
                self.assertEqual(
                    dialog._tabs.tabText(dialog._recovery_tab_index),
                    "Recovery (2)",
                )
                self.assertIs(dialog._tabs.currentWidget(), dialog._recovery_page)
                self.assertEqual(dialog._unlinked_storage_table.rowCount(), 1)
                self.assertEqual(
                    dialog._unlinked_storage_table.item(0, 0).text(),
                    profile_id,
                )
                self.assertEqual(
                    dialog._unlinked_storage_table.item(0, 1).text(),
                    "chromium",
                )
                self.assertEqual(
                    dialog._unlinked_storage_table.item(0, 2).text(),
                    str(managed_path),
                )
                self.assertEqual(dialog._staging_remnants_table.rowCount(), 1)
                self.assertEqual(
                    dialog._staging_remnants_table.item(0, 0).text(),
                    "importing",
                )
                self.assertEqual(
                    dialog._staging_remnants_table.item(0, 1).text(),
                    staging_id,
                )
                self.assertEqual(
                    dialog._staging_remnants_table.item(0, 3).text(),
                    str(staging_path),
                )
                self.assertIn(
                    "partial browser data",
                    dialog._staging_remnants_note.text(),
                )
                self.assertTrue(managed_path.is_dir())
                self.assertTrue(staging_path.is_dir())
            finally:
                dialog.close()

    def test_initial_recovery_attention_yields_to_profile_needing_setup(self) -> None:
        self._legacy_profile(make_folder=False)
        self._unlinked_storage()
        dialog = self._dialog()
        try:
            self.assertEqual(
                dialog._tabs.tabText(dialog._recovery_tab_index),
                "Recovery (1)",
            )
            self.assertIs(dialog._tabs.currentWidget(), dialog._profiles_page)
        finally:
            dialog.close()

    def test_unlinked_storage_can_restore_profile_record_with_same_identity(
        self,
    ) -> None:
        profile_id, managed_path = self._unlinked_storage()
        dialog = self._dialog()
        try:
            dialog._unlinked_storage_table.selectRow(0)
            QApplication.processEvents()
            with (
                patch.object(
                    QInputDialog,
                    "getText",
                    return_value=("Recovered Main", True),
                ),
                patch.object(QMessageBox, "information"),
            ):
                dialog._restore_selected_unlinked_storage()

            record = self.database.get_profile(profile_id)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.name, "Recovered Main")
            self.assertEqual(record.profile_id, profile_id)
            self.assertEqual(record.engine, "chromium")
            self.assertEqual(record.identity.browser, "Chromium")
            self.assertEqual(record.identity.source_profile, "Recovered Work")
            self.assertEqual(record.identity.account_name, "Recovered User")
            self.assertEqual(
                record.identity.account_email,
                "recovered@example.com",
            )
            self.assertTrue(managed_path.is_dir())
            self.assertEqual(dialog._unlinked_storage_table.rowCount(), 0)
            self.assertIs(dialog._tabs.currentWidget(), dialog._profiles_page)
        finally:
            dialog.close()

    def test_unlinked_restore_cancel_and_name_collision_leave_data_unlinked(
        self,
    ) -> None:
        profile_id, managed_path = self._unlinked_storage()
        dialog = self._dialog()
        try:
            dialog._unlinked_storage_table.selectRow(0)
            QApplication.processEvents()
            with patch.object(
                QInputDialog,
                "getText",
                return_value=("", False),
            ):
                dialog._restore_selected_unlinked_storage()
            self.assertIsNone(self.database.get_profile(profile_id))
            self.assertTrue(managed_path.is_dir())

            self.database.create_profile("Already Used")
            with (
                patch.object(
                    QInputDialog,
                    "getText",
                    return_value=("already used", True),
                ),
                patch.object(QMessageBox, "warning") as warning,
            ):
                dialog._restore_selected_unlinked_storage()
            warning.assert_called_once()
            self.assertIsNone(self.database.get_profile(profile_id))
            self.assertTrue(managed_path.is_dir())
        finally:
            dialog.close()

    def test_unlinked_delete_keep_is_non_mutating(self) -> None:
        _profile_id, managed_path = self._unlinked_storage()
        dialog = self._dialog()
        try:
            dialog._unlinked_storage_table.selectRow(0)
            QApplication.processEvents()
            with (
                patch.object(
                    dialog,
                    "_confirm_unlinked_storage_delete",
                    return_value=False,
                ),
                patch(
                    "automadin.ui.browser_profile_import_dialog."
                    "begin_profile_delete"
                ) as begin_delete,
            ):
                dialog._delete_selected_unlinked_storage()

            begin_delete.assert_not_called()
            self.assertTrue(managed_path.is_dir())
            self.assertEqual(dialog._unlinked_storage_table.rowCount(), 1)
        finally:
            dialog.close()

    def test_unlinked_delete_can_be_explicitly_finished(self) -> None:
        _profile_id, managed_path = self._unlinked_storage()
        dialog = self._dialog()
        try:
            dialog._unlinked_storage_table.selectRow(0)
            QApplication.processEvents()
            with (
                patch.object(
                    dialog,
                    "_confirm_unlinked_storage_delete",
                    return_value=True,
                ),
                patch.object(QMessageBox, "information"),
            ):
                dialog._delete_selected_unlinked_storage()

            self.assertFalse(managed_path.exists())
            self.assertEqual(dialog._unlinked_storage_table.rowCount(), 0)
            self.assertEqual(
                dialog._tabs.tabText(dialog._recovery_tab_index),
                "Recovery (0)",
            )
        finally:
            dialog.close()

    def test_unlinked_delete_db_race_restores_storage(self) -> None:
        _profile_id, managed_path = self._unlinked_storage()
        dialog = self._dialog()
        try:
            dialog._unlinked_storage_table.selectRow(0)
            QApplication.processEvents()
            with (
                patch.object(
                    dialog,
                    "_confirm_unlinked_storage_delete",
                    return_value=True,
                ),
                patch.object(
                    self.database,
                    "get_profile",
                    side_effect=(None, object()),
                ),
                patch.object(QMessageBox, "warning") as warning,
            ):
                dialog._delete_selected_unlinked_storage()

            warning.assert_called_once()
            self.assertTrue(managed_path.is_dir())
            self.assertEqual(dialog._unlinked_storage_table.rowCount(), 1)
        finally:
            dialog.close()

    def test_staging_discard_keep_is_non_mutating(self) -> None:
        _profile_id, staging_path = self._staging_remnant("creating")
        dialog = self._dialog()
        try:
            dialog._staging_remnants_table.selectRow(0)
            QApplication.processEvents()
            with (
                patch.object(
                    dialog,
                    "_confirm_staging_discard",
                    return_value=False,
                ),
                patch(
                    "automadin.ui.browser_profile_import_dialog."
                    "discard_profile_staging_remnant"
                ) as discard,
            ):
                dialog._discard_selected_staging_remnant()

            discard.assert_not_called()
            self.assertTrue(staging_path.is_dir())
        finally:
            dialog.close()

    def test_staging_discard_success_and_failure_reporting(self) -> None:
        _profile_id, first_path = self._staging_remnant("creating")
        dialog = self._dialog()
        try:
            dialog._staging_remnants_table.selectRow(0)
            QApplication.processEvents()
            with (
                patch.object(
                    dialog,
                    "_confirm_staging_discard",
                    return_value=True,
                ),
                patch.object(QMessageBox, "information"),
            ):
                dialog._discard_selected_staging_remnant()
            self.assertFalse(first_path.exists())

            _second_id, second_path = self._staging_remnant("importing")
            dialog._refresh_imported_profiles()
            dialog._staging_remnants_table.selectRow(0)
            QApplication.processEvents()
            with (
                patch.object(
                    dialog,
                    "_confirm_staging_discard",
                    return_value=True,
                ),
                patch(
                    "automadin.ui.browser_profile_import_dialog."
                    "discard_profile_staging_remnant",
                    side_effect=RuntimeError("staging changed"),
                ),
                patch.object(QMessageBox, "warning") as warning,
            ):
                dialog._discard_selected_staging_remnant()

            warning.assert_called_once()
            self.assertIn("staging changed", str(warning.call_args.args[2]))
            self.assertIn(str(second_path), str(warning.call_args.args[2]))
            self.assertTrue(second_path.is_dir())
            self.assertEqual(dialog._staging_remnants_table.rowCount(), 1)
        finally:
            dialog.close()

    def test_recovery_inventory_errors_stay_visible_and_block_actions(self) -> None:
        dialog = self._dialog()
        try:
            with (
                patch(
                    "automadin.ui.browser_profile_import_dialog."
                    "list_managed_profile_storage",
                    side_effect=RuntimeError("managed scan denied"),
                ),
                patch(
                    "automadin.ui.browser_profile_import_dialog."
                    "list_profile_staging_remnants",
                    side_effect=RuntimeError("staging scan denied"),
                ),
            ):
                dialog._refresh_imported_profiles()

            self.assertFalse(dialog._unlinked_storage_panel.isHidden())
            self.assertFalse(dialog._staging_remnants_panel.isHidden())
            self.assertIn("managed scan denied", dialog._unlinked_storage_note.text())
            self.assertIn("staging scan denied", dialog._staging_remnants_note.text())
            self.assertFalse(dialog._restore_unlinked_btn.isEnabled())
            self.assertFalse(dialog._delete_unlinked_btn.isEnabled())
            self.assertFalse(dialog._discard_staging_btn.isEnabled())
            self.assertEqual(
                dialog._tabs.tabText(dialog._recovery_tab_index),
                "Recovery (2)",
            )
        finally:
            dialog.close()

    def test_unknown_legacy_engine_can_be_chosen_during_adoption(self) -> None:
        for label, engine in (
            ("Chrome", "chrome"),
            ("Microsoft Edge", "msedge"),
            ("Chromium", "chromium"),
            ("Firefox", "firefox"),
        ):
            with self.subTest(label=label):
                name = f"Unknown {engine}"
                record = self.database.create_profile(
                    name,
                    legacy_storage_name=name,
                )
                legacy = self.profiles_root / record.legacy_storage_name
                legacy.mkdir(parents=True)
                if engine != "firefox":
                    (legacy / "Default").mkdir()
                dialog = self._dialog()
                try:
                    self._select_profile(dialog, record.profile_id)
                    with (
                        patch.object(
                            QInputDialog,
                            "getItem",
                            return_value=(label, True),
                        ),
                        patch.object(dialog, "_confirm_adoption", return_value=True),
                        patch.object(QMessageBox, "information"),
                    ):
                        dialog._adopt_selected_profile()

                    updated = self.database.get_profile(record.profile_id)
                    self.assertIsNotNone(updated)
                    assert updated is not None
                    self.assertEqual(updated.engine, engine)
                    self.assertEqual(updated.legacy_storage_name, "")
                    inspection = inspect_profile_storage(
                        record.profile_id,
                        engine,
                        profiles_root=self.profiles_root,
                    )
                    self.assertEqual(
                        inspection.status,
                        ProfileStorageStatus.READY,
                    )
                    self.assertIsNotNone(inspection.manifest)
                    assert inspection.manifest is not None
                    self.assertEqual(inspection.manifest.engine, engine)
                finally:
                    dialog.close()

    def test_unknown_legacy_engine_choice_cancel_is_non_mutating(self) -> None:
        record = self.database.create_profile(
            "Unknown Cancelled",
            legacy_storage_name="Unknown Cancelled",
        )
        legacy = self.profiles_root / record.legacy_storage_name
        (legacy / "Default").mkdir(parents=True)
        dialog = self._dialog()
        try:
            self._select_profile(dialog, record.profile_id)
            with (
                patch.object(
                    QInputDialog,
                    "getItem",
                    return_value=("Chrome", False),
                ),
                patch.object(dialog, "_confirm_adoption") as confirm,
            ):
                dialog._adopt_selected_profile()

            confirm.assert_not_called()
            self.assertTrue(legacy.is_dir())
            self.assertFalse(
                resolve_profile_storage_path(
                    record.profile_id,
                    self.profiles_root,
                ).exists()
            )
            unchanged = self.database.get_profile(record.profile_id)
            self.assertIsNotNone(unchanged)
            assert unchanged is not None
            self.assertEqual(unchanged.engine, "")
            self.assertEqual(unchanged.legacy_storage_name, record.name)
        finally:
            dialog.close()

    def test_create_uses_uuid_selection_and_rolls_storage_back_on_db_failure(
        self,
    ) -> None:
        dialog = self._dialog()
        try:
            dialog._create_name_edit.setText("Work / Main")
            with (
                patch.object(
                    self.database,
                    "create_profile",
                    side_effect=RuntimeError("database failed"),
                ),
                patch.object(QMessageBox, "warning") as warning,
            ):
                dialog._create_profile()

            warning.assert_called_once()
            self.assertEqual(self.database.list_profiles(), [])
            self.assertEqual(list(self.profiles_root.iterdir()), [])

            with patch.object(QMessageBox, "information"):
                dialog._create_profile()
            record = self.database.get_profile_by_name("Work / Main")
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.legacy_storage_name, "")
            self.assertTrue(
                resolve_profile_storage_path(
                    record.profile_id,
                    self.profiles_root,
                ).is_dir()
            )
            self.assertEqual(dialog._selected_profile_id(), record.profile_id)
        finally:
            dialog.close()

    def test_import_new_profile_compensates_db_failure_and_preserves_source(
        self,
    ) -> None:
        source = self._source_profile()
        profile_id = str(uuid.uuid4())
        change = import_browser_profile(
            source,
            profile_id,
            "chrome",
            profiles_root=self.profiles_root,
        )
        dialog = self._dialog()
        dialog._pending_import = _PendingImport(
            profile_id=profile_id,
            name="Imported Work",
            engine="chrome",
            identity=BrowserProfileIdentity(
                browser="Chrome",
                source_profile="Work",
                account_name="Jane Doe",
                account_email="jane@example.com",
            ),
            existing_profile=False,
        )
        try:
            with (
                patch.object(
                    self.database,
                    "create_profile",
                    side_effect=RuntimeError("database failed"),
                ),
                patch.object(QMessageBox, "warning") as warning,
            ):
                dialog._on_import_succeeded(change)

            warning.assert_called_once()
            self.assertTrue((source / "Preferences").is_file())
            self.assertFalse(
                resolve_profile_storage_path(profile_id, self.profiles_root).exists()
            )
            self.assertEqual(self.database.list_profiles(), [])
        finally:
            dialog.close()

    def test_import_copy_for_unresolved_profile_preserves_uuid_and_source(self) -> None:
        record = self._legacy_profile(make_folder=False)
        source = self._source_profile()
        change = import_browser_profile(
            source,
            record.profile_id,
            "chrome",
            profiles_root=self.profiles_root,
            created_at=record.created_at,
        )
        dialog = self._dialog()
        dialog._pending_import = _PendingImport(
            profile_id=record.profile_id,
            name=record.name,
            engine="chrome",
            identity=BrowserProfileIdentity(
                browser="Chrome",
                source_profile="Work",
                account_name="Jane Doe",
                account_email="jane@example.com",
            ),
            existing_profile=True,
        )
        try:
            with patch.object(QMessageBox, "information"):
                dialog._on_import_succeeded(change)

            updated = self.database.get_profile(record.profile_id)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.profile_id, record.profile_id)
            self.assertEqual(updated.legacy_storage_name, "")
            self.assertEqual(updated.engine, "chrome")
            self.assertTrue((source / "Preferences").is_file())
            self.assertTrue(
                resolve_profile_storage_path(
                    record.profile_id,
                    self.profiles_root,
                ).is_dir()
            )
        finally:
            dialog.close()

    def test_missing_portable_profile_can_import_copy_into_its_existing_uuid(
        self,
    ) -> None:
        record = self.database.create_profile(
            "Portable Work",
            browser="Chrome",
            engine="chrome",
        )
        source = self._source_profile()
        dialog = self._dialog()
        try:
            self._select_profile(dialog, record.profile_id)
            self.assertTrue(dialog._import_copy_btn.isEnabled())
            dialog._configure_selected_profile_import()
            self.assertEqual(dialog._import_target_profile_id, record.profile_id)
            self.assertTrue(dialog._name_edit.isReadOnly())

            change = import_browser_profile(
                source,
                record.profile_id,
                "chrome",
                profiles_root=self.profiles_root,
                created_at=record.created_at,
            )
            dialog._pending_import = _PendingImport(
                profile_id=record.profile_id,
                name=record.name,
                engine="chrome",
                identity=BrowserProfileIdentity(
                    browser="Chrome",
                    source_profile="Work",
                    account_name="Jane Doe",
                    account_email="jane@example.com",
                ),
                existing_profile=True,
            )
            with patch.object(QMessageBox, "information"):
                dialog._on_import_succeeded(change)

            updated = self.database.get_profile(record.profile_id)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.profile_id, record.profile_id)
            self.assertEqual(updated.identity.account_email, "jane@example.com")
            self.assertEqual(
                inspect_profile_storage(
                    record.profile_id,
                    "chrome",
                    profiles_root=self.profiles_root,
                ).status,
                ProfileStorageStatus.READY,
            )
        finally:
            dialog.close()

    def test_custom_source_uses_explicit_browser_choice(self) -> None:
        source = self.root / "custom-profile"
        source.mkdir()
        (source / "Preferences").write_text("{}", encoding="utf-8")
        dialog = self._dialog()
        try:
            dialog._source_edit.setText(str(source))
            dialog._name_edit.setText("Custom Work")
            self.assertEqual(dialog._source_browser_combo.currentData(), "")

            dialog._source_browser_combo.setCurrentIndex(
                dialog._source_browser_combo.findData("Chrome")
            )
            with (
                patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.Yes,
                ),
                patch(
                    "automadin.ui.browser_profile_import_dialog._CopyProfileThread"
                ) as thread_type,
            ):
                dialog._start_import()

            self.assertEqual(thread_type.call_args.args[0], str(source))
            self.assertEqual(thread_type.call_args.args[2], "chrome")
            self.assertEqual(dialog._pending_import.identity.browser, "Chrome")
        finally:
            dialog._thread = None
            dialog.close()

    def test_all_dialog_dismissal_paths_wait_for_queued_copy_result(self) -> None:
        dialog = self._dialog()
        try:
            dialog.setResult(QDialog.DialogCode.Accepted)
            dialog._thread = MagicMock()

            dialog.reject()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            dialog.done(QDialog.DialogCode.Rejected)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            event = MagicMock()
            dialog.closeEvent(event)
            event.ignore.assert_called_once()

            dialog._thread = None
            dialog.done(QDialog.DialogCode.Rejected)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        finally:
            dialog._thread = None
            dialog.close()

    def test_new_import_rejects_casefold_collision_before_starting_copy(self) -> None:
        self.database.create_profile("Work")
        source = self._source_profile()
        dialog = self._dialog()
        try:
            dialog._source_edit.setText(str(source))
            dialog._identity = BrowserProfileIdentity(browser="Chrome")
            dialog._name_edit.setText("wOrK")
            with (
                patch.object(QMessageBox, "warning") as warning,
                patch(
                    "automadin.ui.browser_profile_import_dialog._CopyProfileThread"
                ) as thread_type,
            ):
                dialog._start_import()

            warning.assert_called_once()
            thread_type.assert_not_called()
            self.assertTrue((source / "Preferences").is_file())
            self.assertFalse(
                any(
                    path.name.startswith(".importing-")
                    for path in self.profiles_root.iterdir()
                )
            )
        finally:
            dialog.close()

    def test_unresolved_delete_removes_only_db_row_and_keeps_legacy_candidate(
        self,
    ) -> None:
        record = self._legacy_profile()
        legacy_file = (
            self.profiles_root
            / record.legacy_storage_name
            / "Default"
            / "Preferences"
        )
        dialog = self._dialog()
        try:
            self._select_profile(dialog, record.profile_id)
            with (
                patch.object(
                    dialog,
                    "_confirm_profile_deletion",
                    return_value=True,
                ),
                patch.object(QMessageBox, "information"),
            ):
                dialog._delete_selected_profile()

            self.assertIsNone(self.database.get_profile(record.profile_id))
            self.assertTrue(legacy_file.is_file())
        finally:
            dialog.close()

    def test_ready_delete_is_in_use_blocked_then_tombstoned_and_removed(self) -> None:
        record = self._ready_profile()
        config = SavedRunConfig(
            name="Daily",
            group_path="Games/IOM",
            flow_name="Collect",
            device_id=BrowserDeviceConfig(
                BrowserEngine.CHROMIUM,
                profile_id=record.profile_id,
            ).to_id(),
        )
        self.database.save_run_config(config.name, config)
        managed_path = resolve_profile_storage_path(
            record.profile_id,
            self.profiles_root,
        )
        dialog = self._dialog()
        try:
            self._select_profile(dialog, record.profile_id)
            with (
                patch.object(QMessageBox, "warning") as warning,
                patch.object(dialog, "_confirm_profile_deletion") as confirm,
            ):
                dialog._delete_selected_profile()
            warning.assert_called_once()
            confirm.assert_not_called()
            self.assertTrue(managed_path.is_dir())

            self.database.delete_run_config(config.name)
            with (
                patch.object(
                    dialog,
                    "_confirm_profile_deletion",
                    return_value=True,
                ),
                patch.object(QMessageBox, "information"),
            ):
                dialog._delete_selected_profile()
            self.assertIsNone(self.database.get_profile(record.profile_id))
            self.assertFalse(managed_path.exists())
        finally:
            dialog.close()

    def test_live_borrow_blocks_profile_deletion_and_names_active_use(self) -> None:
        registry = BrowserProfileLeaseRegistry()
        record = self._ready_profile()
        owner = registry.acquire_owner(
            record.profile_id,
            ProfileUse("selector", "Design browser", "owner-1"),
        )
        borrow = owner.borrow(
            ProfileUse("run-step", "Run Step debugger", "borrow-1"),
        )
        dialog = self._dialog(registry)
        try:
            self._select_profile(dialog, record.profile_id)
            with (
                patch.object(
                    dialog,
                    "_confirm_profile_deletion",
                    return_value=True,
                ),
                patch(
                    "automadin.ui.browser_profile_import_dialog.begin_profile_delete"
                ) as begin_delete,
                patch.object(QMessageBox, "warning") as warning,
            ):
                dialog._delete_selected_profile()

            begin_delete.assert_not_called()
            self.assertIsNotNone(self.database.get_profile(record.profile_id))
            warning.assert_called_once()
            self.assertIn("Run Step debugger", warning.call_args.args[2])
        finally:
            borrow.release()
            owner.release()
            dialog.close()

    def test_real_copy_outcome_waits_for_qthread_finished_before_database_commit(
        self,
    ) -> None:
        registry = BrowserProfileLeaseRegistry()
        dialog = self._dialog(registry)
        self._prepare_copy(dialog, name="Real Deferred Work")
        entered = threading.Event()
        release = threading.Event()
        commit_observations: list[str] = []

        def copy_profile(
            _source,
            profile_id,
            engine,
            **_kwargs,
        ) -> ProfileStorageChange:
            entered.set()
            self.assertTrue(release.wait(2))

            def commit() -> None:
                snapshot = registry.snapshot(profile_id)
                self.assertIsNotNone(snapshot)
                assert snapshot is not None
                self.assertEqual(snapshot.mode, "maintenance")
                commit_observations.append("commit")

            return ProfileStorageChange(
                operation="import",
                profile_id=profile_id,
                engine=engine,
                path=self.profiles_root / profile_id,
                working_path=self.profiles_root / f".importing-{profile_id}",
                _lease_registry=_kwargs["lease_registry"],
                _maintenance_lease=_kwargs["maintenance_lease"],
                _commit_action=commit,
            )

        with (
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(QMessageBox, "information"),
            patch.object(
                profile_dialog,
                "import_browser_profile",
                side_effect=copy_profile,
            ),
        ):
            dialog._start_import()
            self.assertTrue(entered.wait(1))
            pending = dialog._pending_import
            self.assertIsNotNone(pending)
            assert pending is not None
            worker = dialog._thread
            self.assertIsNotNone(worker)
            assert worker is not None

            release.set()
            deadline = time.monotonic() + 2
            while worker.isRunning():
                if time.monotonic() >= deadline:
                    self.fail("Real profile copy worker did not finish")
                time.sleep(0.005)

            # The native worker is done, but queued result/finished delivery has
            # not run. Database registration and maintenance release stay deferred.
            self.assertIsNone(self.database.get_profile(pending.profile_id))
            self.assertIs(dialog._thread, worker)
            self.assertIsNotNone(registry.snapshot(pending.profile_id))
            self.assertEqual(commit_observations, [])

            deadline = time.monotonic() + 2
            while dialog._thread is not None:
                QApplication.processEvents()
                if time.monotonic() >= deadline:
                    self.fail("Queued profile copy completion was not delivered")

        self.assertEqual(commit_observations, ["commit"])
        self.assertIsNotNone(self.database.get_profile(pending.profile_id))
        self.assertIsNone(registry.snapshot(pending.profile_id))

    def test_fake_failure_outcome_is_deferred_and_never_waits_on_gui_thread(
        self,
    ) -> None:
        registry = BrowserProfileLeaseRegistry()
        dialog = self._dialog(registry)
        self._prepare_copy(dialog, name="Fake Deferred Failure")
        failure_message = (
            "The browser profile could not be copied. Review Diagnostics for "
            "details, then try again."
        )

        with (
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(QMessageBox, "warning") as warning,
            patch.object(profile_dialog, "_CopyProfileThread", _DeferredCopyThread),
        ):
            dialog._start_import()
            worker = _DeferredCopyThread.instance
            self.assertIsNotNone(worker)
            assert worker is not None
            pending = dialog._pending_import
            self.assertIsNotNone(pending)
            assert pending is not None

            worker.failed.emit(failure_message)

            warning.assert_not_called()
            self.assertIs(dialog._thread, worker)
            self.assertIsNotNone(dialog._pending_import)
            self.assertIsNotNone(registry.snapshot(pending.profile_id))

            worker.finish()

        warning.assert_called_once_with(
            dialog,
            "Profile Copy Failed",
            failure_message,
        )
        self.assertEqual(worker.wait_calls, 0)
        self.assertEqual(worker.deleted, 1)
        self.assertIsNone(dialog._thread)
        self.assertIsNone(registry.snapshot(pending.profile_id))

    def test_database_failure_rolls_back_before_maintenance_release(self) -> None:
        registry = BrowserProfileLeaseRegistry()
        dialog = self._dialog(registry)
        self._prepare_copy(dialog, name="Rollback Deferred Work")
        rollback_observations: list[str] = []

        with (
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(QMessageBox, "warning") as warning,
            patch.object(profile_dialog, "_CopyProfileThread", _DeferredCopyThread),
        ):
            dialog._start_import()
            worker = _DeferredCopyThread.instance
            self.assertIsNotNone(worker)
            assert worker is not None
            pending = dialog._pending_import
            self.assertIsNotNone(pending)
            assert pending is not None

            def rollback() -> None:
                snapshot = registry.snapshot(pending.profile_id)
                self.assertIsNotNone(snapshot)
                assert snapshot is not None
                self.assertEqual(snapshot.mode, "maintenance")
                rollback_observations.append("rollback")

            change = ProfileStorageChange(
                operation="import",
                profile_id=pending.profile_id,
                engine=pending.engine,
                path=self.profiles_root / pending.profile_id,
                working_path=self.profiles_root / f".importing-{pending.profile_id}",
                _lease_registry=registry,
                _maintenance_lease=dialog._pending_import_lease,
                _rollback_action=rollback,
            )
            worker.succeeded.emit(change)
            self.assertEqual(rollback_observations, [])
            self.assertIsNotNone(registry.snapshot(pending.profile_id))

            with (
                patch.object(
                    self.database,
                    "create_profile",
                    side_effect=RuntimeError("SENTINEL_PRIVATE_DB_FAILURE"),
                ),
                self.assertLogs(profile_dialog.logger, level=logging.ERROR) as logs,
            ):
                worker.finish()

        self.assertEqual(rollback_observations, ["rollback"])
        self.assertIsNone(registry.snapshot(pending.profile_id))
        warning.assert_called_once()
        public_message = warning.call_args.args[2]
        self.assertNotIn("SENTINEL", public_message)
        self.assertTrue(any(record.exc_info for record in logs.records))
        self.assertEqual(worker.wait_calls, 0)

    def test_copy_worker_failure_logs_traceback_and_emits_fixed_message(self) -> None:
        failure_messages: list[str] = []
        profile_id = str(uuid.uuid4())
        registry, lease = _maintenance(profile_id)
        worker = profile_dialog._CopyProfileThread(
            str(self.root / "source"),
            profile_id,
            "chrome",
            maintenance_lease=lease,
            lease_registry=registry,
        )
        worker.failed.connect(failure_messages.append)

        with (
            patch.object(
                profile_dialog,
                "import_browser_profile",
                side_effect=RuntimeError("SENTINEL_PRIVATE_COPY_FAILURE"),
            ),
            self.assertLogs(profile_dialog.logger, level=logging.ERROR) as logs,
        ):
            worker.run()

        self.assertEqual(
            failure_messages,
            [
                "The browser profile could not be copied. Review Diagnostics for "
                "details, then try again."
            ],
        )
        self.assertNotIn("SENTINEL", failure_messages[0])
        self.assertTrue(any(record.exc_info for record in logs.records))

    def test_copy_start_failure_rolls_back_ui_and_lease_with_safe_message(
        self,
    ) -> None:
        class _StartFailureThread(_DeferredCopyThread):
            def start(self) -> None:
                raise RuntimeError("SENTINEL_PRIVATE_START_FAILURE")

        registry = BrowserProfileLeaseRegistry()
        dialog = self._dialog(registry)
        self._prepare_copy(dialog, name="Start Failure Work")

        with (
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(QMessageBox, "warning") as warning,
            patch.object(profile_dialog, "_CopyProfileThread", _StartFailureThread),
            self.assertLogs(profile_dialog.logger, level=logging.ERROR) as logs,
        ):
            dialog._start_import()

        worker = _StartFailureThread.instance
        self.assertIsNotNone(worker)
        assert worker is not None
        self.assertIsNone(registry.snapshot(worker.profile_id))
        self.assertIsNone(dialog._thread)
        self.assertIsNone(dialog._pending_import)
        self.assertTrue(dialog._import_btn.isEnabled())
        self.assertTrue(dialog._close_btn.isEnabled())
        warning.assert_called_once()
        self.assertEqual(warning.call_args.args[1], "Profile Copy Could Not Start")
        self.assertNotIn("SENTINEL", warning.call_args.args[2])
        self.assertTrue(any(record.exc_info for record in logs.records))
        self.assertEqual(worker.wait_calls, 0)
        self.assertEqual(worker.deleted, 1)

    def test_start_raises_after_running_retains_worker_and_lease_until_finish(
        self,
    ) -> None:
        class _UncertainStartThread(_DeferredCopyThread):
            def start(self) -> None:
                self.running = True
                raise RuntimeError("SENTINEL_PRIVATE_UNCERTAIN_START")

        registry = BrowserProfileLeaseRegistry()
        dialog = self._dialog(registry)
        self._prepare_copy(dialog, name="Uncertain Start Work")
        failure_message = (
            "The browser profile could not be copied. Review Diagnostics for "
            "details, then try again."
        )

        with (
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(QMessageBox, "warning") as warning,
            patch.object(profile_dialog, "_CopyProfileThread", _UncertainStartThread),
            self.assertLogs(profile_dialog.logger, level=logging.ERROR) as logs,
        ):
            dialog._start_import()

        worker = _UncertainStartThread.instance
        self.assertIsNotNone(worker)
        assert worker is not None
        self.assertIs(dialog._thread, worker)
        self.assertIsNotNone(registry.snapshot(worker.profile_id))
        self.assertFalse(dialog._import_btn.isEnabled())
        self.assertFalse(dialog._close_btn.isEnabled())
        warning.assert_called_once()
        self.assertNotIn("SENTINEL", warning.call_args.args[2])
        self.assertTrue(any(record.exc_info for record in logs.records))

        worker.failed.emit(failure_message)
        with patch.object(QMessageBox, "warning"):
            worker.finish()

        self.assertIsNone(dialog._thread)
        self.assertIsNone(registry.snapshot(worker.profile_id))
        self.assertEqual(worker.wait_calls, 0)
        self.assertEqual(worker.deleted, 1)

    def test_worker_and_maintenance_release_errors_are_contained(self) -> None:
        class _ReleaseFailureLease:
            def __init__(self) -> None:
                self.release_calls = 0

            def release(self) -> None:
                self.release_calls += 1
                raise RuntimeError("SENTINEL_PRIVATE_LEASE_RELEASE")

        class _ReleaseFailureRegistry:
            def __init__(self) -> None:
                self.lease = _ReleaseFailureLease()

            def acquire_maintenance(self, *_args, **_kwargs):
                return self.lease

        class _DeleteFailureThread(_DeferredCopyThread):
            def deleteLater(self) -> None:
                self.deleted += 1
                raise RuntimeError("SENTINEL_PRIVATE_WORKER_RELEASE")

        registry = _ReleaseFailureRegistry()
        dialog = self._dialog(registry)  # type: ignore[arg-type]
        self._prepare_copy(dialog, name="Cleanup Failure Work")
        failure_message = (
            "The browser profile could not be copied. Review Diagnostics for "
            "details, then try again."
        )

        with (
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(QMessageBox, "warning"),
            patch.object(profile_dialog, "_CopyProfileThread", _DeleteFailureThread),
        ):
            dialog._start_import()
            worker = _DeleteFailureThread.instance
            self.assertIsNotNone(worker)
            assert worker is not None
            worker.failed.emit(failure_message)
            with self.assertLogs(
                profile_dialog.logger,
                level=logging.ERROR,
            ) as logs:
                worker.finish()

        self.assertIsNone(dialog._thread)
        self.assertIs(dialog._pending_import_lease, registry.lease)
        self.assertTrue(dialog._import_btn.isEnabled())
        self.assertEqual(worker.wait_calls, 0)
        self.assertEqual(worker.deleted, 1)
        self.assertEqual(registry.lease.release_calls, 1)
        self.assertGreaterEqual(
            sum(record.exc_info is not None for record in logs.records),
            2,
        )

    def test_applied_import_reports_cleanup_uncertain_and_blocks_close(self) -> None:
        class _ReleaseFailureLease:
            def release(self) -> None:
                raise RuntimeError("release failed")

        dialog = self._dialog()
        profile_id = str(uuid.uuid4())
        dialog._pending_import_lease = _ReleaseFailureLease()  # type: ignore[assignment]
        dialog._pending_import = _PendingImport(
            profile_id=profile_id,
            name="Applied Import",
            engine="chrome",
            identity=BrowserProfileIdentity(browser="Chrome"),
            existing_profile=False,
        )

        class _Change:
            path = self.profiles_root / profile_id
            active = True

            def commit(change_self) -> None:
                change_self.active = False

            def rollback(_change_self) -> None:
                self.fail("applied import must not roll back")

        with (
            patch.object(QMessageBox, "information") as information,
            patch.object(QMessageBox, "warning") as warning,
        ):
            dialog._on_import_succeeded(_Change())  # type: ignore[arg-type]

        information.assert_not_called()
        self.assertTrue(
            any(call.args[1] == "Profile Saved; Cleanup Uncertain" for call in warning.call_args_list)
        )
        self.assertTrue(dialog._has_active_worker())

    def test_applied_create_reports_cleanup_uncertain(self) -> None:
        class _ReleaseFailureLease:
            def release(self) -> None:
                raise RuntimeError("release failed")

        dialog = self._dialog()
        dialog._create_name_edit.setText("Created Despite Cleanup")
        retained = _ReleaseFailureLease()

        def create_record(*_args, **_kwargs):
            self.database.create_profile(
                "Created Despite Cleanup",
                profile_id=str(uuid.uuid4()),
                browser="Chromium",
                engine="chromium",
            )
            return self.profiles_root / "created", retained

        with (
            patch.object(profile_dialog, "_create_managed_profile", side_effect=create_record),
            patch.object(QMessageBox, "information") as information,
            patch.object(QMessageBox, "warning") as warning,
        ):
            dialog._create_profile()

        information.assert_not_called()
        warning.assert_called_once()
        self.assertEqual(warning.call_args.args[1], "Profile Saved; Cleanup Uncertain")
        self.assertTrue(dialog.profiles_changed)
        self.assertTrue(dialog._has_active_worker())

    def test_failed_create_retains_live_lease_when_release_also_fails(self) -> None:
        class _Lease:
            released = False

            def release(self) -> None:
                raise RuntimeError("release failed")

        lease = _Lease()
        error = profile_dialog._ManagedProfileCreationError(
            RuntimeError("create failed"),
            lease,  # type: ignore[arg-type]
        )
        dialog = self._dialog()
        dialog._create_name_edit.setText("Failed Create")

        with (
            patch.object(profile_dialog, "_create_managed_profile", side_effect=error),
            patch.object(QMessageBox, "warning") as warning,
        ):
            dialog._create_profile()

        self.assertIn(lease, dialog._retained_maintenance_leases)
        self.assertTrue(dialog._has_active_worker())
        self.assertEqual(
            [call.args[1] for call in warning.call_args_list],
            ["Profile Creation Failed", "Profile Cleanup Uncertain"],
        )

    def test_create_helper_carries_live_lease_across_body_and_release_failure(self) -> None:
        class _Lease:
            released = False

            def release(self) -> None:
                raise RuntimeError("release failed")

        lease = _Lease()

        class _Registry:
            @staticmethod
            def acquire_maintenance(*_args, **_kwargs):
                return lease

        with (
            patch.object(
                profile_dialog,
                "create_clean_profile",
                side_effect=RuntimeError("create failed"),
            ),
            self.assertLogs(profile_dialog.logger, level=logging.ERROR),
            self.assertRaises(profile_dialog._ManagedProfileCreationError) as raised,
        ):
            profile_dialog._create_managed_profile(
                self.database,
                "Failed Helper Create",
                "chromium",
                lease_registry=_Registry(),  # type: ignore[arg-type]
            )

        self.assertIs(raised.exception.maintenance_lease, lease)

    def test_import_receipt_failure_compensates_new_database_mutation(self) -> None:
        registry = BrowserProfileLeaseRegistry()
        dialog = self._dialog(registry)
        profile_id = str(uuid.uuid4())
        dialog._pending_import_lease = registry.acquire_maintenance(
            profile_id,
            ProfileUse("maintenance", "Import", "import-order"),
        )
        dialog._pending_import = _PendingImport(
            profile_id=profile_id,
            name="Commit Failure",
            engine="chrome",
            identity=BrowserProfileIdentity(browser="Chrome"),
            existing_profile=False,
        )

        class _Change:
            path = self.profiles_root / profile_id
            working_path = path
            active = True

            @staticmethod
            def commit() -> None:
                raise RuntimeError("commit failed")

            def rollback(change_self) -> None:
                change_self.active = False

        with (
            patch.object(self.database, "create_profile", wraps=self.database.create_profile) as create,
            patch.object(QMessageBox, "warning"),
            self.assertLogs(profile_dialog.logger, level=logging.ERROR),
        ):
            dialog._on_import_succeeded(_Change())  # type: ignore[arg-type]

        create.assert_called_once()
        self.assertIsNone(self.database.get_profile(profile_id))

    def test_import_receipt_failure_restores_existing_database_snapshot(self) -> None:
        record = self.database.create_profile(
            "Existing Commit Failure",
            legacy_storage_name="Existing Commit Failure",
            browser="Chrome",
            source_profile="Old",
            account_name="Old User",
            account_email="old@example.com",
            engine="chrome",
        )
        registry = BrowserProfileLeaseRegistry()
        dialog = self._dialog(registry)
        dialog._pending_import_lease = registry.acquire_maintenance(
            record.profile_id,
            ProfileUse("maintenance", "Import", "existing-import-order"),
        )
        dialog._pending_import = _PendingImport(
            profile_id=record.profile_id,
            name=record.name,
            engine="chrome",
            identity=BrowserProfileIdentity(
                browser="Chrome",
                source_profile="New",
                account_name="New User",
                account_email="new@example.com",
            ),
            existing_profile=True,
        )

        class _Change:
            path = self.profiles_root / record.profile_id
            working_path = path
            active = True

            @staticmethod
            def commit() -> None:
                raise RuntimeError("commit failed")

            def rollback(change_self) -> None:
                change_self.active = False

        with (
            patch.object(QMessageBox, "warning"),
            self.assertLogs(profile_dialog.logger, level=logging.ERROR),
        ):
            dialog._on_import_succeeded(_Change())  # type: ignore[arg-type]

        self.assertEqual(self.database.get_profile(record.profile_id), record)

    def test_import_compensation_does_not_clobber_concurrent_profile_change(self) -> None:
        record = self.database.create_profile(
            "Concurrent Commit Failure",
            legacy_storage_name="Concurrent Commit Failure",
            browser="Chrome",
            account_name="Original User",
            engine="chrome",
        )
        registry = BrowserProfileLeaseRegistry()
        dialog = self._dialog(registry)
        dialog._pending_import_lease = registry.acquire_maintenance(
            record.profile_id,
            ProfileUse("maintenance", "Import", "concurrent-import"),
        )
        dialog._pending_import = _PendingImport(
            profile_id=record.profile_id,
            name=record.name,
            engine="chrome",
            identity=BrowserProfileIdentity(browser="Chrome", account_name="Imported User"),
            existing_profile=True,
        )

        class _Change:
            path = self.profiles_root / record.profile_id
            working_path = path
            active = True

            def commit(change_self) -> None:
                self.database.update_profile_metadata(
                    record.profile_id,
                    account_name="Concurrent User",
                )
                raise RuntimeError("commit failed")

            def rollback(change_self) -> None:
                change_self.active = False

        with (
            patch.object(QMessageBox, "warning") as warning,
            self.assertLogs(profile_dialog.logger, level=logging.ERROR),
        ):
            dialog._on_import_succeeded(_Change())  # type: ignore[arg-type]

        current = self.database.get_profile(record.profile_id)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.identity.account_name, "Concurrent User")
        self.assertTrue(dialog.profiles_changed)
        self.assertIn("Recovery tab", warning.call_args.args[2])

    def test_existing_import_snapshot_read_failure_still_releases_lease(self) -> None:
        profile_id = str(uuid.uuid4())
        registry = BrowserProfileLeaseRegistry()
        dialog = self._dialog(registry)
        dialog._pending_import_lease = registry.acquire_maintenance(
            profile_id,
            ProfileUse("maintenance", "Import", "snapshot-read-failure"),
        )
        dialog._pending_import = _PendingImport(
            profile_id=profile_id,
            name="Snapshot Failure",
            engine="chrome",
            identity=BrowserProfileIdentity(browser="Chrome"),
            existing_profile=True,
        )

        class _Change:
            path = self.profiles_root / profile_id
            working_path = path
            active = True

            def rollback(change_self) -> None:
                change_self.active = False

        with (
            patch.object(self.database, "get_profile", side_effect=RuntimeError("read failed")),
            patch.object(QMessageBox, "warning"),
            self.assertLogs(profile_dialog.logger, level=logging.ERROR),
        ):
            dialog._on_import_succeeded(_Change())  # type: ignore[arg-type]

        self.assertIsNone(registry.snapshot(profile_id))
        self.assertIsNone(dialog._pending_import_lease)

    def test_adopt_receipt_failure_restores_exact_database_snapshot(self) -> None:
        record = self.database.create_profile(
            "Legacy Commit Failure",
            legacy_storage_name="Legacy Commit Failure",
            engine="chrome",
        )
        dialog = self._dialog()
        inspection = SimpleNamespace(
            recovery_path=None,
            status=ProfileStorageStatus.NEEDS_SETUP,
            engine="chrome",
            manifest=None,
            legacy_candidate=self.profiles_root / record.legacy_storage_name,
            path=self.profiles_root / record.profile_id,
        )
        dialog._profile_inspections[record.profile_id] = inspection

        class _Change:
            active = True

            @staticmethod
            def commit() -> None:
                raise RuntimeError("commit failed")

            def rollback(change_self) -> None:
                change_self.active = False

        with (
            patch.object(dialog, "_selected_profile_record", return_value=record),
            patch.object(dialog, "_confirm_adoption", return_value=True),
            patch.object(profile_dialog, "adopt_legacy_profile", return_value=_Change()),
            patch.object(
                self.database,
                "mark_profile_storage_resolved",
                wraps=self.database.mark_profile_storage_resolved,
            ) as resolve,
            patch.object(QMessageBox, "warning"),
        ):
            dialog._adopt_selected_profile()

        resolve.assert_called_once()
        self.assertEqual(
            self.database.get_profile(record.profile_id),
            record,
        )

    def test_lost_import_context_surfaces_rollback_and_release_uncertainty(self) -> None:
        class _Lease:
            released = False

            def release(self) -> None:
                raise RuntimeError("release failed")

        class _Thread:
            @staticmethod
            def deleteLater() -> None:
                return None

        recovery_path = self.profiles_root / ".importing-lost-context"

        class _Change:
            working_path = recovery_path

            @staticmethod
            def rollback() -> None:
                raise RuntimeError("rollback failed")

        dialog = self._dialog()
        dialog._thread = _Thread()  # type: ignore[assignment]
        dialog._pending_import_lease = _Lease()  # type: ignore[assignment]

        with (
            patch.object(QMessageBox, "warning") as warning,
            self.assertLogs(profile_dialog.logger, level=logging.ERROR),
        ):
            dialog._on_import_succeeded(_Change())  # type: ignore[arg-type]

        self.assertEqual(
            [call.args[1] for call in warning.call_args_list],
            ["Profile Copy Recovery Required", "Profile Cleanup Uncertain"],
        )
        self.assertIn(str(recovery_path), warning.call_args_list[0].args[2])
        self.assertTrue(dialog._has_active_worker())

    def test_delete_commit_cleanup_failure_is_warning_with_recovery_path(self) -> None:
        record = self._ready_profile()
        dialog = self._dialog()
        self._select_profile(dialog, record.profile_id)
        recovery_path = self.profiles_root / f".deleting-{record.profile_id}"

        class _Change:
            active = True
            working_path = recovery_path

            @staticmethod
            def commit() -> None:
                raise RuntimeError("protected cleanup failed")

            @staticmethod
            def rollback() -> None:
                raise AssertionError("database deletion succeeded")

        with (
            patch.object(dialog, "_confirm_profile_deletion", return_value=True),
            patch.object(profile_dialog, "begin_profile_delete", return_value=_Change()),
            patch.object(QMessageBox, "information") as information,
            patch.object(QMessageBox, "warning") as warning,
        ):
            dialog._delete_selected_profile()

        information.assert_not_called()
        warning.assert_called_once()
        self.assertEqual(warning.call_args.args[1], "Profile Deleted; Cleanup Incomplete")
        self.assertIn(str(recovery_path), warning.call_args.args[2])
        self.assertIsNone(self.database.get_profile(record.profile_id))

    def test_applied_staging_discard_reports_cleanup_uncertain(self) -> None:
        class _ReleaseFailureLease:
            def release(self) -> None:
                raise RuntimeError("release failed")

        dialog = self._dialog()
        remnant = SimpleNamespace(
            profile_id=str(uuid.uuid4()),
            operation=SimpleNamespace(value="creating"),
            path=self.profiles_root / ".creating-remnant",
        )
        with (
            patch.object(dialog, "_selected_staging_remnant", return_value=remnant),
            patch.object(dialog, "_confirm_staging_discard", return_value=True),
            patch.object(dialog, "_acquire_maintenance", return_value=_ReleaseFailureLease()),
            patch.object(profile_dialog, "discard_profile_staging_remnant"),
            patch.object(QMessageBox, "information") as information,
            patch.object(QMessageBox, "warning") as warning,
        ):
            dialog._discard_selected_staging_remnant()

        information.assert_not_called()
        warning.assert_called_once()
        self.assertEqual(warning.call_args.args[1], "Profile Saved; Cleanup Uncertain")
        self.assertTrue(dialog._has_active_worker())

    def test_import_holds_maintenance_lease_until_async_failure_finishes(self) -> None:
        registry = BrowserProfileLeaseRegistry()
        dialog = self._dialog(registry)
        source = self.root / "source"
        source.mkdir()
        dialog._source_edit.setText(str(source))
        dialog._name_edit.setText("Async Work")
        browser_index = dialog._source_browser_combo.findData("Chrome")
        self.assertGreaterEqual(browser_index, 0)
        dialog._source_browser_combo.setCurrentIndex(browser_index)

        observed: dict[str, object] = {}

        class _Signal:
            def connect(self, _callback) -> None:
                return None

        class _Thread:
            def __init__(
                thread_self,
                _source,
                profile_id,
                _engine,
                **_kwargs,
            ) -> None:
                thread_self.profile_id = profile_id
                thread_self.succeeded = _Signal()
                thread_self.failed = _Signal()

            def start(thread_self) -> None:
                observed["snapshot"] = registry.snapshot(thread_self.profile_id)
                with self.assertRaises(BrowserProfileInUseError):
                    registry.acquire_owner(
                        thread_self.profile_id,
                        ProfileUse("launcher", "Launcher run", "owner-2"),
                    )

            def wait(self) -> None:
                return None

            def deleteLater(self) -> None:
                return None

        with (
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch(
                "automadin.ui.browser_profile_import_dialog._CopyProfileThread",
                _Thread,
            ),
        ):
            dialog._start_import()

        snapshot = observed["snapshot"]
        self.assertEqual(snapshot.mode, "maintenance")
        pending = dialog._pending_import
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertIsNotNone(registry.snapshot(pending.profile_id))

        with patch.object(QMessageBox, "warning"):
            dialog._on_import_failed("copy failed")
        self.assertIsNone(registry.snapshot(pending.profile_id))

    def test_import_holds_maintenance_through_database_finalize_and_commit(
        self,
    ) -> None:
        registry = BrowserProfileLeaseRegistry()
        dialog = self._dialog(registry)
        profile_id = str(uuid.uuid4())
        lease = registry.acquire_maintenance(
            profile_id,
            ProfileUse("maintenance", "Async import", "import-1"),
        )
        dialog._pending_import_lease = lease
        dialog._pending_import = _PendingImport(
            profile_id=profile_id,
            name="Committed Async Work",
            engine="chrome",
            identity=BrowserProfileIdentity(browser="Chrome"),
            existing_profile=False,
        )

        class _Thread:
            @staticmethod
            def wait() -> None:
                return None

            @staticmethod
            def deleteLater() -> None:
                return None

        observed: list[str] = []

        class _Change:
            path = self.profiles_root / profile_id
            active = True

            def commit(change_self) -> None:
                snapshot = registry.snapshot(profile_id)
                self.assertIsNotNone(snapshot)
                assert snapshot is not None
                self.assertEqual(snapshot.mode, "maintenance")
                with self.assertRaises(BrowserProfileInUseError):
                    registry.acquire_owner(
                        profile_id,
                        ProfileUse("launcher", "Competing run", "run-1"),
                    )
                observed.append("commit")
                change_self.active = False

            def rollback(_change_self) -> None:
                self.fail("successful import must not roll back")

        dialog._thread = _Thread()
        with patch.object(QMessageBox, "information"):
            dialog._on_import_succeeded(_Change())

        self.assertEqual(observed, ["commit"])
        self.assertIsNotNone(self.database.get_profile(profile_id))
        self.assertIsNone(registry.snapshot(profile_id))
        self.assertTrue(lease.released)

    def test_ready_delete_rolls_storage_back_when_db_delete_fails(self) -> None:
        record = self._ready_profile()
        managed_path = resolve_profile_storage_path(
            record.profile_id,
            self.profiles_root,
        )
        dialog = self._dialog()
        try:
            self._select_profile(dialog, record.profile_id)
            with (
                patch.object(
                    dialog,
                    "_confirm_profile_deletion",
                    return_value=True,
                ),
                patch.object(
                    self.database,
                    "delete_profile_by_id",
                    return_value=False,
                ),
                patch.object(QMessageBox, "warning") as warning,
            ):
                dialog._delete_selected_profile()

            warning.assert_called_once()
            self.assertTrue(managed_path.is_dir())
            self.assertIsNotNone(self.database.get_profile(record.profile_id))
        finally:
            dialog.close()

    def test_corrupt_or_mismatched_uuid_storage_is_blocked_from_deletion(self) -> None:
        record = self.database.create_profile("Corrupt", engine="chromium")
        managed_path = resolve_profile_storage_path(
            record.profile_id,
            self.profiles_root,
        )
        (managed_path / "Default").mkdir(parents=True)
        (managed_path / "profile.json").write_text("{broken", encoding="utf-8")
        dialog = self._dialog()
        try:
            self._select_profile(dialog, record.profile_id)
            with (
                patch.object(QMessageBox, "warning") as warning,
                patch.object(dialog, "_confirm_profile_deletion") as confirm,
            ):
                dialog._delete_selected_profile()

            warning.assert_called_once()
            confirm.assert_not_called()
            self.assertIsNotNone(self.database.get_profile(record.profile_id))
            self.assertTrue(managed_path.is_dir())
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
