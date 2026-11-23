import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.dom import minidom

from automadin.models.android_file import AndroidFilePreview
from automadin.services.android_file_preview import preview_local_file, preview_text


class AndroidFilePreviewTests(unittest.TestCase):
    def test_preview_text_pretty_prints_json(self) -> None:
        preview = preview_text("/tmp/state.json", '{"b":2,"a":1}', truncated=False)

        self.assertIsInstance(preview, AndroidFilePreview)
        self.assertEqual(preview.path, "/tmp/state.json")
        self.assertEqual(preview.kind, "json")
        self.assertIn('"a": 1', preview.text)
        self.assertIn('"b": 2', preview.text)

    def test_preview_text_keeps_xml_readable(self) -> None:
        preview = preview_text("/tmp/prefs.xml", '<map><string name="PFGame">x</string></map>', truncated=False)

        self.assertEqual(preview.kind, "xml")
        self.assertIn("PFGame", preview.text)

    def test_preview_text_skips_xml_with_doctype_entities(self) -> None:
        text = '<!DOCTYPE foo [<!ENTITY x "boom">]><foo>&x;</foo>'
        with patch("automadin.services.android_file_preview.minidom.parseString") as parse_string:
            parse_string.side_effect = AssertionError("unsafe XML should not be parsed")
            preview = preview_text("/tmp/prefs.xml", text, truncated=False)

        parse_string.assert_not_called()
        self.assertEqual(preview.kind, "xml")
        self.assertIn("<!DOCTYPE", preview.text)

    def test_preview_text_falls_back_when_json_formatting_recurses(self) -> None:
        with patch("automadin.services.android_file_preview.json.dumps", side_effect=RecursionError):
            preview = preview_text("/tmp/state.json", "[[]]", truncated=False)

        self.assertEqual(preview.kind, "text")
        self.assertEqual(preview.text, "[[]]")

    def test_preview_text_falls_back_when_xml_formatting_recurses(self) -> None:
        with patch.object(minidom.Document, "toprettyxml", side_effect=RecursionError):
            preview = preview_text("/tmp/prefs.xml", "<map/>", truncated=False)

        self.assertEqual(preview.kind, "xml")
        self.assertIn("<map/>", preview.text)

    def test_preview_local_file_identifies_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "blob.bin"
            path.write_bytes(b"\x00\x01\x02\x03")

            preview = preview_local_file("/device/blob.bin", str(path))

        self.assertEqual(preview.kind, "binary")
        self.assertIn("Binary file", preview.text)

    def test_preview_local_file_detects_binary_in_sentinel_byte(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "blob.bin"
            path.write_bytes(b"abc\x00")

            preview = preview_local_file("/device/blob.bin", str(path), max_bytes=3)

        self.assertEqual(preview.kind, "binary")
        self.assertTrue(preview.truncated)

    def test_preview_local_file_detects_binary_after_first_4096_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "blob.bin"
            path.write_bytes((b"a" * 4097) + b"\x00")

            preview = preview_local_file("/device/blob.bin", str(path), max_bytes=5000)

        self.assertEqual(preview.kind, "binary")
        self.assertFalse(preview.truncated)

    def test_preview_local_file_allows_extensionless_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config"
            path.write_text('{"enabled":true}', encoding="utf-8")

            preview = preview_local_file("/device/config", str(path))

        self.assertEqual(preview.kind, "json")
        self.assertIn('"enabled": true', preview.text)

    def test_preview_local_file_allows_extensionless_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "readme"
            path.write_text("plain config", encoding="utf-8")

            preview = preview_local_file("/device/readme", str(path))

        self.assertEqual(preview.kind, "text")
        self.assertEqual(preview.text, "plain config")

    def test_preview_local_file_opens_sqlite_paths_with_uri_characters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state#100%.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE saves (id INTEGER)")
                connection.commit()
            finally:
                connection.close()

            preview = preview_local_file("/device/state.db", str(path))

        self.assertEqual(preview.kind, "sqlite")
        self.assertIn("saves", preview.text)

    def test_preview_local_file_caps_sqlite_object_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.db"
            connection = sqlite3.connect(path)
            try:
                for index in range(201):
                    connection.execute(f"CREATE TABLE table_{index:03d} (id INTEGER)")
                connection.commit()
            finally:
                connection.close()

            preview = preview_local_file("/device/state.db", str(path))

        self.assertEqual(preview.kind, "sqlite")
        self.assertTrue(preview.truncated)
        self.assertIn("[Preview truncated after 200 objects]", preview.text)
        self.assertIn("table_000", preview.text)

    def test_preview_local_file_does_not_cap_exact_sqlite_object_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.db"
            connection = sqlite3.connect(path)
            try:
                for index in range(200):
                    connection.execute(f"CREATE TABLE table_{index:03d} (id INTEGER)")
                connection.commit()
            finally:
                connection.close()

            preview = preview_local_file("/device/state.db", str(path))

        self.assertEqual(preview.kind, "sqlite")
        self.assertFalse(preview.truncated)
        self.assertNotIn("[Preview truncated after 200 objects]", preview.text)
        self.assertIn("table_000", preview.text)
        self.assertIn("table_199", preview.text)

    def test_preview_local_file_reads_only_preview_window(self) -> None:
        class FakeBinaryFile:
            def __init__(self) -> None:
                self.read_sizes: list[int] = []

            def __enter__(self) -> "FakeBinaryFile":
                return self

            def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                self.read_sizes.append(size)
                return b"abcdef"

        fake_file = FakeBinaryFile()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "notes.txt"
            path.write_text("abcdef", encoding="utf-8")

            with patch.object(Path, "open", return_value=fake_file):
                preview = preview_local_file("/device/notes.txt", str(path), max_bytes=5)

        self.assertEqual(fake_file.read_sizes, [6])
        self.assertEqual(preview.kind, "text")
        self.assertTrue(preview.truncated)
        self.assertTrue(preview.text.startswith("abcde"))


if __name__ == "__main__":
    unittest.main()
