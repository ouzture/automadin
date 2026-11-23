from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QDialog

from automadin.services.browser_profile_readiness import (
    BrowserProfileReadiness,
    BrowserProfileReadinessState,
)
from automadin.services.database import DatabaseService
from automadin.ui import browser_profile_import_dialog as profile_dialog
from automadin.ui.browser_profile_import_dialog import BrowserProfileImportDialog


class _Signal:
    def __init__(self) -> None:
        self._callbacks = []

    def connect(self, callback) -> None:
        self._callbacks.append(callback)

    def emit(self, *args) -> None:
        for callback in tuple(self._callbacks):
            callback(*args)


class _DeferredReadinessThread:
    instances: list[_DeferredReadinessThread] = []

    def __init__(self, data_dir, parent=None) -> None:
        self.data_dir = Path(data_dir)
        self.parent = parent
        self.succeeded = _Signal()
        self.failed = _Signal()
        self.finished = _Signal()
        self.running = False
        self.deleted = 0
        self.wait_calls = 0
        type(self).instances.append(self)

    def start(self) -> None:
        self.running = True

    def isRunning(self) -> bool:
        return self.running

    def wait(self, *_args) -> bool:
        self.wait_calls += 1
        raise AssertionError("Readiness UI must not wait on the GUI thread")

    def deleteLater(self) -> None:
        self.deleted += 1

    def complete(self, results: dict[str, BrowserProfileReadiness]) -> None:
        self.succeeded.emit(results)
        self.running = False
        self.finished.emit()


def _readiness(
    engine: str,
    state: BrowserProfileReadinessState,
    message: str | None = None,
) -> BrowserProfileReadiness:
    if message is None and state is BrowserProfileReadinessState.UNAVAILABLE:
        if engine in {"chromium", "firefox"}:
            message = (
                f"{engine.title()} is unavailable. Open Tools > Runtime Components "
                "and install or repair Playwright browsers."
            )
        else:
            vendor = "Google Chrome" if engine == "chrome" else "Microsoft Edge"
            message = f"Install {vendor} before using this profile."
    return BrowserProfileReadiness(
        engine=engine,
        state=state,
        message=message or f"{engine} {state.value}",
        path=Path(f"C:/{engine}.exe") if state is BrowserProfileReadinessState.AVAILABLE else None,
        source=("system" if state is BrowserProfileReadinessState.AVAILABLE else state.value),
    )


def _result_set(
    *,
    chrome: BrowserProfileReadinessState = BrowserProfileReadinessState.AVAILABLE,
    msedge: BrowserProfileReadinessState = BrowserProfileReadinessState.AVAILABLE,
    chromium: BrowserProfileReadinessState = BrowserProfileReadinessState.AVAILABLE,
    firefox: BrowserProfileReadinessState = BrowserProfileReadinessState.AVAILABLE,
) -> dict[str, BrowserProfileReadiness]:
    return {
        "chromium": _readiness("chromium", chromium),
        "firefox": _readiness("firefox", firefox),
        "chrome": _readiness("chrome", chrome),
        "msedge": _readiness("msedge", msedge),
    }


class BrowserProfileRuntimeReadinessUiTests(unittest.TestCase):
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
        self.dialogs: list[BrowserProfileImportDialog] = []
        _DeferredReadinessThread.instances = []

    def tearDown(self) -> None:
        for dialog in self.dialogs:
            dialog._readiness_thread = None
            dialog._thread = None
            dialog.close()
            dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()
        self.database.close()
        self._environment.stop()
        self._temporary_directory.cleanup()

    def _dialog(self, *, opener=None) -> BrowserProfileImportDialog:
        dialog = BrowserProfileImportDialog(
            self.database,
            data_dir=self.root,
            open_runtime_components=opener,
        )
        self.dialogs.append(dialog)
        return dialog

    @staticmethod
    def _select_profile(
        dialog: BrowserProfileImportDialog,
        profile_id: str,
    ) -> None:
        for row in range(dialog._profiles_table.rowCount()):
            item = dialog._profiles_table.item(row, 0)
            if item.data(Qt.ItemDataRole.UserRole) == profile_id:
                dialog._profiles_table.selectRow(row)
                QApplication.processEvents()
                return
        raise AssertionError(f"Profile {profile_id} was not listed")

    @staticmethod
    def _row_for(dialog: BrowserProfileImportDialog, name: str) -> int:
        for row in range(dialog._profiles_table.rowCount()):
            if dialog._profiles_table.item(row, 0).text() == name:
                return row
        raise AssertionError(f"Profile {name!r} was not listed")

    def test_refresh_is_retained_until_exact_finished_signal(self) -> None:
        dialog = self._dialog()
        results = _result_set()

        with patch.object(
            profile_dialog,
            "_ProfileReadinessThread",
            _DeferredReadinessThread,
        ):
            dialog._request_readiness_refresh()
            worker = _DeferredReadinessThread.instances[-1]
            worker.succeeded.emit(results)

            self.assertIs(dialog._readiness_thread, worker)
            self.assertEqual(dialog._readiness_cache, {})
            self.assertEqual(worker.deleted, 0)
            self.assertEqual(worker.wait_calls, 0)

            worker.running = False
            worker.finished.emit()

        self.assertIsNone(dialog._readiness_thread)
        self.assertEqual(dialog._readiness_cache, results)
        self.assertEqual(worker.deleted, 1)
        self.assertEqual(worker.wait_calls, 0)

    def test_first_show_starts_one_refresh_but_repeated_show_does_not(self) -> None:
        dialog = self._dialog()
        with patch.object(
            profile_dialog,
            "_ProfileReadinessThread",
            _DeferredReadinessThread,
        ):
            dialog.show()
            QApplication.processEvents()
            self.assertEqual(len(_DeferredReadinessThread.instances), 1)
            worker = _DeferredReadinessThread.instances[-1]
            worker.complete(_result_set())

            dialog.hide()
            dialog.show()
            QApplication.processEvents()

        self.assertEqual(len(_DeferredReadinessThread.instances), 1)
        self.assertTrue(dialog._readiness_started)

    def test_profiles_table_keeps_profile_data_and_runtime_distinct(self) -> None:
        available = self.database.create_profile(
            "Available",
            browser="Chrome",
            engine="chrome",
        )
        unavailable = self.database.create_profile(
            "Unavailable",
            browser="Edge",
            engine="msedge",
        )
        unknown = self.database.create_profile(
            "Unverified",
            browser="Firefox",
            engine="firefox",
        )
        unknown_engine = self.database.create_profile("Unknown Engine")
        dialog = self._dialog()
        dialog._readiness_cache = _result_set(
            msedge=BrowserProfileReadinessState.UNAVAILABLE,
            firefox=BrowserProfileReadinessState.UNKNOWN,
        )
        dialog._refresh_imported_profiles()

        headers = [
            dialog._profiles_table.horizontalHeaderItem(column).text()
            for column in range(dialog._profiles_table.columnCount())
        ]
        self.assertEqual(dialog._profiles_table.columnCount(), 8)
        self.assertEqual(headers[1:3], ["Profile data", "Runtime"])
        expected = {
            available.profile_id: "Available",
            unavailable.profile_id: "Missing",
            unknown.profile_id: "Could not check",
            unknown_engine.profile_id: "Unknown engine",
        }
        for profile_id, runtime_text in expected.items():
            self._select_profile(dialog, profile_id)
            row = dialog._profiles_table.currentRow()
            self.assertIn(
                dialog._profiles_table.item(row, 1).text(),
                {
                    "Needs setup",
                    "Profile data missing",
                    "Inspection failed",
                },
            )
            self.assertEqual(dialog._profiles_table.item(row, 2).text(), runtime_text)

    def test_unavailable_is_nonblocking_and_repair_action_is_exact(self) -> None:
        opened: list[str] = []
        dialog = self._dialog(opener=lambda: opened.append("opened"))
        dialog._readiness_cache = _result_set(
            chromium=BrowserProfileReadinessState.UNAVAILABLE,
            chrome=BrowserProfileReadinessState.UNAVAILABLE,
        )

        dialog._create_engine_combo.setCurrentIndex(
            dialog._create_engine_combo.findData("chromium")
        )
        dialog._source_browser_combo.setCurrentIndex(
            dialog._source_browser_combo.findData("Chromium")
        )
        dialog._update_runtime_presentations()
        dialog._update_controls()

        self.assertTrue(dialog._create_btn.isEnabled())
        self.assertTrue(dialog._import_btn.isEnabled())
        self.assertIn(
            "Runtime Components",
            dialog._create_runtime_status.text(),
        )
        self.assertFalse(dialog._create_runtime_manager_btn.isHidden())
        self.assertFalse(dialog._source_runtime_manager_btn.isHidden())
        dialog._source_runtime_manager_btn.click()
        self.assertEqual(opened, ["opened"])

        dialog._source_browser_combo.setCurrentIndex(
            dialog._source_browser_combo.findData("Chrome")
        )
        dialog._update_runtime_presentations()
        dialog._update_controls()

        self.assertTrue(dialog._import_btn.isEnabled())
        self.assertIn("Install Google Chrome", dialog._source_runtime_status.text())
        self.assertTrue(dialog._source_runtime_manager_btn.isHidden())

    def test_source_browser_and_repair_target_use_exact_engine(self) -> None:
        record = self.database.create_profile(
            "Edge Repair",
            browser="Edge",
            engine="msedge",
        )
        dialog = self._dialog()
        dialog._source_browser_combo.setCurrentIndex(
            dialog._source_browser_combo.findData("Chrome")
        )
        self.assertEqual(dialog._desired_import_engine(), "chrome")

        self._select_profile(dialog, record.profile_id)
        self.assertTrue(dialog._import_copy_btn.isEnabled())
        dialog._configure_selected_profile_import()

        self.assertEqual(dialog._import_target_profile_id, record.profile_id)
        self.assertEqual(dialog._source_browser_combo.currentData(), "Edge")
        self.assertEqual(dialog._desired_import_engine(), "msedge")

    def test_unknown_engine_repair_clears_stale_source_browser(self) -> None:
        record = self.database.create_profile("Unknown Repair")
        dialog = self._dialog()
        dialog._source_browser_combo.setCurrentIndex(
            dialog._source_browser_combo.findData("Chrome")
        )
        self._select_profile(dialog, record.profile_id)

        dialog._configure_selected_profile_import()

        self.assertEqual(dialog._import_target_profile_id, record.profile_id)
        self.assertEqual(dialog._source_browser_combo.currentData(), "")
        self.assertEqual(dialog._desired_import_engine(), "")
        self.assertIn("not known", dialog._source_runtime_status.text())

    def test_selected_profile_runtime_detail_refreshes_with_async_result(self) -> None:
        record = self.database.create_profile(
            "Selected Chrome",
            browser="Chrome",
            engine="chrome",
        )
        dialog = self._dialog()
        self._select_profile(dialog, record.profile_id)
        self.assertIn("Runtime: Not checked", dialog._selected_profile_details.text())

        dialog._readiness_cache = _result_set(chrome=BrowserProfileReadinessState.UNAVAILABLE)
        dialog._update_runtime_presentations()

        self.assertIn("Runtime: Missing", dialog._selected_profile_details.text())
        row = dialog._profiles_table.currentRow()
        self.assertEqual(dialog._profiles_table.item(row, 2).text(), "Missing")

    def test_coalesced_refresh_discards_first_and_second_result_wins(self) -> None:
        first_results = _result_set(chrome=BrowserProfileReadinessState.UNAVAILABLE)
        second_results = _result_set(msedge=BrowserProfileReadinessState.UNAVAILABLE)
        dialog = self._dialog()

        with patch.object(
            profile_dialog,
            "_ProfileReadinessThread",
            _DeferredReadinessThread,
        ):
            dialog._request_readiness_refresh()
            first = _DeferredReadinessThread.instances[-1]
            dialog._request_readiness_refresh()

            self.assertTrue(dialog._readiness_refresh_pending)
            self.assertEqual(len(_DeferredReadinessThread.instances), 1)

            first.complete(first_results)
            self.assertEqual(len(_DeferredReadinessThread.instances), 2)
            second = _DeferredReadinessThread.instances[-1]
            self.assertFalse(first.running)
            self.assertTrue(second.running)
            self.assertEqual(dialog._readiness_cache, {})

            second.complete(second_results)

        self.assertIsNone(dialog._readiness_thread)
        self.assertEqual(dialog._readiness_cache, second_results)
        self.assertEqual(first.deleted, 1)
        self.assertEqual(second.deleted, 1)
        self.assertEqual(first.wait_calls + second.wait_calls, 0)

    def test_failed_refresh_clears_stale_success_only_at_exact_finish(self) -> None:
        record = self.database.create_profile(
            "Previously Available",
            browser="Chrome",
            engine="chrome",
        )
        dialog = self._dialog()
        dialog._readiness_cache = _result_set()
        dialog._refresh_imported_profiles()
        row = self._row_for(dialog, record.name)
        self.assertEqual(dialog._profiles_table.item(row, 2).text(), "Available")
        private_detail = r"C:\Users\person\secret-runtime-path"

        with patch.object(
            profile_dialog,
            "_ProfileReadinessThread",
            _DeferredReadinessThread,
        ):
            dialog._request_readiness_refresh()
            worker = _DeferredReadinessThread.instances[-1]
            worker.failed.emit(profile_dialog._READINESS_FAILURE_MESSAGE)

            self.assertIs(dialog._readiness_thread, worker)
            self.assertTrue(dialog._readiness_cache)
            self.assertEqual(dialog._profiles_table.item(row, 2).text(), "Checking…")

            worker.running = False
            worker.finished.emit()

        self.assertIsNone(dialog._readiness_thread)
        self.assertEqual(dialog._readiness_cache, {})
        self.assertEqual(dialog._profiles_table.item(row, 2).text(), "Could not check")
        public_detail = dialog._profiles_table.item(row, 2).toolTip()
        self.assertEqual(public_detail, profile_dialog._READINESS_FAILURE_MESSAGE)
        self.assertNotIn(private_detail, public_detail)
        self.assertNotIn(private_detail, dialog._create_runtime_status.text())
        self.assertTrue(dialog._create_btn.isEnabled())
        self.assertTrue(dialog._import_btn.isEnabled())
        self.assertTrue(dialog._close_btn.isEnabled())
        self.assertEqual(worker.deleted, 1)
        self.assertEqual(worker.wait_calls, 0)

    def test_certain_start_failure_releases_worker_and_remains_usable(self) -> None:
        class _CertainStartFailure(_DeferredReadinessThread):
            def start(self) -> None:
                raise RuntimeError("SENTINEL_PRIVATE_START_FAILURE")

        dialog = self._dialog()
        with (
            patch.object(
                profile_dialog,
                "_ProfileReadinessThread",
                _CertainStartFailure,
            ),
            self.assertLogs(profile_dialog.logger, level="ERROR") as logs,
        ):
            dialog._request_readiness_refresh()

        worker = _CertainStartFailure.instances[-1]
        self.assertIsNone(dialog._readiness_thread)
        self.assertEqual(worker.deleted, 1)
        self.assertEqual(worker.wait_calls, 0)
        self.assertTrue(dialog._close_btn.isEnabled())
        self.assertTrue(dialog._create_btn.isEnabled())
        self.assertTrue(dialog._import_btn.isEnabled())
        self.assertNotIn("SENTINEL", dialog._create_runtime_status.text())
        self.assertTrue(any(record.exc_info for record in logs.records))

    def test_uncertain_start_failure_retains_worker_until_exact_finish(self) -> None:
        class _UncertainStartFailure(_DeferredReadinessThread):
            def start(self) -> None:
                self.running = True
                raise RuntimeError("SENTINEL_PRIVATE_UNCERTAIN_START")

        dialog = self._dialog()
        with (
            patch.object(
                profile_dialog,
                "_ProfileReadinessThread",
                _UncertainStartFailure,
            ),
            self.assertLogs(profile_dialog.logger, level="ERROR"),
        ):
            dialog._request_readiness_refresh()

        worker = _UncertainStartFailure.instances[-1]
        self.assertIs(dialog._readiness_thread, worker)
        self.assertFalse(dialog._close_btn.isEnabled())
        self.assertEqual(worker.deleted, 0)
        self.assertEqual(worker.wait_calls, 0)

        worker.complete(_result_set())

        self.assertIsNone(dialog._readiness_thread)
        self.assertEqual(worker.deleted, 1)
        self.assertTrue(dialog._close_btn.isEnabled())
        self.assertEqual(worker.wait_calls, 0)

    def test_all_close_paths_guard_a_retained_readiness_worker(self) -> None:
        dialog = self._dialog()
        dialog.setResult(QDialog.DialogCode.Accepted)
        worker = MagicMock()
        dialog._readiness_thread = worker

        dialog.reject()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        dialog.done(QDialog.DialogCode.Rejected)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        event = MagicMock()
        dialog.closeEvent(event)
        event.ignore.assert_called_once()

        dialog._readiness_thread = None
        dialog.done(QDialog.DialogCode.Rejected)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)


if __name__ == "__main__":
    unittest.main()
