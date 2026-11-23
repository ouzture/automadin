import unittest
from types import SimpleNamespace
from unittest.mock import patch

from automadin.engine.adb_engine import AdbDeviceEngine
from automadin.models.android_file import AndroidFileEntry, AndroidRootState


class AndroidFileModelTests(unittest.TestCase):
    def test_android_file_entry_display_size_formats_directories(self) -> None:
        entry = AndroidFileEntry(
            path="/data/user/0/com.example",
            name="com.example",
            is_dir=True,
            size=4096,
            mode="drwx------",
            owner="u0_a57",
            group="u0_a57",
            modified="2026-05-14 13:54",
        )

        self.assertEqual(entry.display_size, "<DIR>")

    def test_android_file_entry_display_size_formats_files(self) -> None:
        entry = AndroidFileEntry(
            path="/data/user/0/com.example/file.xml",
            name="file.xml",
            is_dir=False,
            size=80820,
            mode="-rw-rw----",
            owner="u0_a57",
            group="u0_a57",
            modified="2026-05-14 13:54",
        )

        self.assertEqual(entry.display_size, "78.9 KB")
        self.assertTrue(entry.is_regular_file)

    def test_android_file_entry_distinguishes_symlink_from_regular_file(self) -> None:
        entry = AndroidFileEntry(
            path="/sdcard",
            name="sdcard",
            is_dir=False,
            size=21,
            mode="lrwxrwxrwx",
            owner="root",
            group="root",
            modified="2026-05-14 13:54",
        )

        self.assertFalse(entry.is_regular_file)

    def test_root_state_needs_root_when_uid_is_shell(self) -> None:
        state = AndroidRootState(uid=2000, username="shell", raw="uid=2000(shell)")

        self.assertFalse(state.is_root)
        self.assertEqual(state.label, "shell")


class AdbFilesystemHelperTests(unittest.TestCase):
    def test_foreground_package_reads_resumed_activity(self) -> None:
        engine = AdbDeviceEngine(adb_path="adb")
        result = SimpleNamespace(
            stdout=(
                "mResumedActivity: ActivityRecord{123 u0 "
                "com.example.automation/.MainActivity t52}"
            ),
            stderr="",
            returncode=0,
        )

        with patch("automadin.engine.adb_engine.subprocess.run", return_value=result):
            package_name = engine.foreground_package({"device_id": "localhost:5557"})

        self.assertEqual(package_name, "com.example.automation")

    def test_foreground_package_reads_top_resumed_activity(self) -> None:
        engine = AdbDeviceEngine(adb_path="adb")
        result = SimpleNamespace(
            stdout=(
                "topResumedActivity=ActivityRecord{621b53e u0 "
                "com.example.automation/.MainActivity t54}"
            ),
            stderr="",
            returncode=0,
        )

        with patch("automadin.engine.adb_engine.subprocess.run", return_value=result):
            package_name = engine.foreground_package({"device_id": "localhost:5557"})

        self.assertEqual(package_name, "com.example.automation")

    def test_foreground_package_returns_none_when_no_package_found(self) -> None:
        engine = AdbDeviceEngine(adb_path="adb")
        result = SimpleNamespace(stdout="mResumedActivity: <none>", stderr="", returncode=0)

        with patch("automadin.engine.adb_engine.subprocess.run", return_value=result):
            package_name = engine.foreground_package({"device_id": "localhost:5557"})

        self.assertIsNone(package_name)

    def test_send_home_uses_keyevent_without_shell_wrapper(self) -> None:
        engine = AdbDeviceEngine(adb_path="adb")
        result = SimpleNamespace(stdout=b"", stderr=b"", returncode=0)

        with patch("automadin.engine.adb_engine.subprocess.run", return_value=result) as run:
            engine.send_home({"device_id": "localhost:5557"})

        self.assertEqual(
            run.call_args.args[0],
            ["adb", "-s", "localhost:5557", "shell", "input", "keyevent", "3"],
        )

    def test_launch_package_uses_monkey_launcher_category(self) -> None:
        engine = AdbDeviceEngine(adb_path="adb")
        result = SimpleNamespace(stdout="Events injected: 1", stderr="", returncode=0)

        with patch("automadin.engine.adb_engine.subprocess.run", return_value=result) as run:
            engine.launch_package(
                {"device_id": "localhost:5557"},
                "com.example.automation",
            )

        self.assertEqual(
            run.call_args.args[0],
            [
                "adb",
                "-s",
                "localhost:5557",
                "shell",
                "monkey",
                "-p",
                "com.example.automation",
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ],
        )

    def test_parse_root_state_reads_root_uid_and_username(self) -> None:
        state = AdbDeviceEngine.parse_root_state(
            "uid=0(root) gid=0(root) groups=0(root),1004(input)"
        )

        self.assertTrue(state.is_root)
        self.assertEqual(state.uid, 0)
        self.assertEqual(state.username, "root")

    def test_parse_ls_long_line_reads_directory_entry(self) -> None:
        entry = AdbDeviceEngine.parse_ls_long_line(
            "drwx------ 13 u0_a57 u0_a57 4096 2026-05-10 23:54 shared_prefs",
            "/data/user/0/com.example.automation",
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.name, "shared_prefs")
        self.assertTrue(entry.is_dir)
        self.assertEqual(
            entry.path,
            "/data/user/0/com.example.automation/shared_prefs",
        )

    def test_parse_ls_long_line_reads_file_entry_with_spaces(self) -> None:
        entry = AdbDeviceEngine.parse_ls_long_line(
            "-rw-rw---- 1 u0_a57 u0_a57 80820 2026-05-14 13:54 file with spaces.xml",
            "/data/local/tmp",
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.name, "file with spaces.xml")
        self.assertFalse(entry.is_dir)
        self.assertEqual(entry.size, 80820)

    def test_parse_ls_long_line_strips_symlink_target(self) -> None:
        entry = AdbDeviceEngine.parse_ls_long_line(
            "lrwxrwxrwx 1 root root 21 2026-05-14 13:54 sdcard -> /storage/self/primary",
            "/",
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.name, "sdcard")
        self.assertEqual(entry.path, "/sdcard")
        self.assertFalse(entry.is_dir)
        self.assertFalse(entry.is_regular_file)

    def test_list_directory_passes_single_remote_shell_command(self) -> None:
        engine = AdbDeviceEngine(adb_path="adb")
        result = SimpleNamespace(
            stdout=(
                "drwx------ 13 u0_a57 u0_a57 4096 2026-05-10 23:54 shared_prefs\n"
            ),
            stderr="",
            returncode=0,
        )

        with patch("automadin.engine.adb_engine.subprocess.run", return_value=result) as run:
            entries = engine.list_directory(
                {"device_id": "localhost:5557"},
                "/data/user/0/com.example.automation",
            )

        self.assertEqual(len(entries), 1)
        self.assertEqual(
            run.call_args.args[0],
            [
                "adb",
                "-s",
                "localhost:5557",
                "shell",
                "ls -la '/data/user/0/com.example.automation'",
            ],
        )

    def test_read_file_text_preserves_whitespace_and_detects_truncation(self) -> None:
        engine = AdbDeviceEngine(adb_path="adb")
        result = SimpleNamespace(stdout=b"abc\n", stderr=b"", returncode=0)

        with patch("automadin.engine.adb_engine.subprocess.run", return_value=result) as run:
            text, truncated = engine.read_file_text(
                {"device_id": "localhost:5557"}, "/tmp/file", max_bytes=3
            )

        self.assertEqual(text, "abc")
        self.assertTrue(truncated)
        self.assertIn("head -c 4", run.call_args.args[0][-1])
        self.assertNotIn("sh", run.call_args.args[0])

    def test_read_file_text_preserves_trailing_newline_when_not_truncated(self) -> None:
        engine = AdbDeviceEngine(adb_path="adb")
        result = SimpleNamespace(stdout=b"abc\n", stderr=b"", returncode=0)

        with patch("automadin.engine.adb_engine.subprocess.run", return_value=result):
            text, truncated = engine.read_file_text(
                {"device_id": "localhost:5557"}, "/tmp/file", max_bytes=4
            )

        self.assertEqual(text, "abc\n")
        self.assertFalse(truncated)


if __name__ == "__main__":
    unittest.main()
