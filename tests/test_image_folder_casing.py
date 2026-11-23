from __future__ import annotations

import base64
from io import BytesIO
import os
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QApplication

from automadin.models.run_config import SavedRunConfig
from automadin.services.database import DatabaseService
from automadin.services.find_image import MatchResult
from automadin.services.screen_text_options import SCREEN_TEXT_RESULT_TYPE_CHOICES
from automadin.ui.flow_editor.image_picker import ImagePickerDialog
from automadin.ui.utility.utility_view import UtilityView


_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNg"
    "YAAAAAMAASsJTYQAAAAASUVORK5CYII="
)


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _database_with_legacy_image(path: Path) -> DatabaseService:
    db = DatabaseService(str(path))
    db.save_run_config(
        "Phone",
        SavedRunConfig(
            name="Phone",
            group_path="IOM",
            flow_name="iom/click_fire_bomb",
            device_id="android:test",
        ),
    )
    db.save_image("iom/Phone button", _ONE_PIXEL_PNG, 1, 1)
    return db


def test_vision_lab_uses_shared_screen_text_result_types(tmp_path: Path) -> None:
    _app()
    db = _database_with_legacy_image(tmp_path / "utility-options.db")
    view = UtilityView(db, None)
    try:
        actual = [
            (
                view._ocr_result_type_combo.itemData(index),
                view._ocr_result_type_combo.itemText(index),
            )
            for index in range(view._ocr_result_type_combo.count())
        ]
        assert actual == [
            (choice.value, choice.label)
            for choice in SCREEN_TEXT_RESULT_TYPE_CHOICES
        ]
        assert view._ocr_result_type_combo.findData("duration_ms") >= 0
    finally:
        view.close()
        view.deleteLater()
        db.close()


def test_image_picker_uses_canonical_folder_but_preserves_raw_identity() -> None:
    _app()
    with tempfile.TemporaryDirectory() as temp_dir:
        db = _database_with_legacy_image(Path(temp_dir) / "picker.db")
        dialog = ImagePickerDialog(db)

        assert dialog._group_filter._groups == ["IOM"]
        assert dialog._image_list.topLevelItemCount() == 1
        group = dialog._image_list.topLevelItem(0)
        assert group.text(0) == "IOM"
        image = group.child(0)
        assert image.data(0, Qt.ItemDataRole.UserRole) == "iom/Phone button"
        assert image.toolTip(0) == "IOM/Phone button"
        assert db.list_images()[0]["name"] == "iom/Phone button"

        dialog.close()
        dialog.deleteLater()
        db.close()


def test_vision_lab_uses_the_same_canonical_image_folder() -> None:
    _app()
    with tempfile.TemporaryDirectory() as temp_dir:
        db = _database_with_legacy_image(Path(temp_dir) / "utility.db")
        view = UtilityView(db, None)

        assert view._group_filter._groups == ["IOM"]
        assert view._image_list.topLevelItemCount() == 1
        group = view._image_list.topLevelItem(0)
        assert group.text(0) == "IOM"
        image = group.child(0)
        assert image.data(0, Qt.ItemDataRole.UserRole) == "iom/Phone button"
        assert image.toolTip(0) == "IOM/Phone button"
        assert view._crop_group_combo.findText("IOM") >= 0

        view.close()
        view.deleteLater()
        db.close()


def test_vision_lab_tests_a_selected_template_inside_a_fixed_search_scope() -> None:
    _app()
    with tempfile.TemporaryDirectory() as temp_dir:
        db = _database_with_legacy_image(Path(temp_dir) / "utility-scope.db")
        view = UtilityView(db, None)
        screenshot = BytesIO()
        Image.new("RGBA", (100, 80), "white").save(screenshot, format="PNG")
        view.set_screenshot(screenshot.getvalue())
        group = view._image_list.topLevelItem(0)
        view._image_list.setCurrentItem(group.child(0))
        view._search_region_mode_combo.setCurrentIndex(
            view._search_region_mode_combo.findData("fixed")
        )
        view._search_start_x_spin.setValue(10)
        view._search_start_y_spin.setValue(20)
        view._search_end_x_spin.setValue(60)
        view._search_end_y_spin.setValue(70)
        seen_sizes: list[tuple[int, int]] = []

        def find_all(search_png, *_args):
            with Image.open(BytesIO(search_png)) as cropped:
                seen_sizes.append(cropped.size)
            return [MatchResult(5, 6, 10, 10, 0.95)]

        view._find_service.find_all = find_all
        view._find_on_screen()

        assert seen_sizes == [(50, 50)]
        assert "(20, 31)" in view._match_label.text()
        assert view._clear_overlays_btn.isEnabled()

        view.close()
        view.deleteLater()
        db.close()


def test_vision_lab_ocr_test_reads_the_selected_crop_without_flow_state(
    monkeypatch,
) -> None:
    _app()
    with tempfile.TemporaryDirectory() as temp_dir:
        db = _database_with_legacy_image(Path(temp_dir) / "utility-ocr.db")
        view = UtilityView(db, None)
        screenshot = BytesIO()
        Image.new("RGBA", (100, 80), "white").save(screenshot, format="PNG")
        view.set_screenshot(screenshot.getvalue())
        view._on_crop_selected(QRect(10, 20, 30, 15))
        view._ocr_result_type_combo.setCurrentIndex(
            view._ocr_result_type_combo.findData("number")
        )
        view._ocr_decimal_separator_combo.setCurrentIndex(
            view._ocr_decimal_separator_combo.findData("comma")
        )
        view._ocr_preprocessing_combo.setCurrentIndex(
            view._ocr_preprocessing_combo.findData("upscale_contrast")
        )
        seen_sizes: list[tuple[int, int]] = []

        class Engine:
            def recognize_png(self, png: bytes) -> str:
                with Image.open(BytesIO(png)) as crop:
                    seen_sizes.append(crop.size)
                return "  1,25  "

        class ImmediatePool:
            def start(self, task) -> None:
                task.run()

        view._ocr_engine = Engine()
        monkeypatch.setattr(
            "automadin.ui.utility.utility_view.QThreadPool.globalInstance",
            lambda: ImmediatePool(),
        )
        view._test_ocr_crop()

        assert seen_sizes == [(60, 30)]
        assert "1,25" in view._ocr_result.toPlainText()
        assert "Parsed Decimal number: 1.25" in view._ocr_result.toPlainText()
        assert "2× grayscale + contrast" in view._ocr_result.toPlainText()
        assert "No flow Variables were changed" in view._ocr_result.toPlainText()
        assert view._ocr_crop_preview.pixmap() is not None

        view.close()
        view.deleteLater()
        db.close()


def test_vision_lab_ocr_can_reuse_an_entered_fixed_search_scope(monkeypatch) -> None:
    _app()
    with tempfile.TemporaryDirectory() as temp_dir:
        db = _database_with_legacy_image(Path(temp_dir) / "utility-ocr-scope.db")
        view = UtilityView(db, None)
        screenshot = BytesIO()
        Image.new("RGBA", (100, 80), "white").save(screenshot, format="PNG")
        view.set_screenshot(screenshot.getvalue())
        view._ocr_region_source_combo.setCurrentIndex(
            view._ocr_region_source_combo.findData("search_scope")
        )
        view._search_region_mode_combo.setCurrentIndex(
            view._search_region_mode_combo.findData("fixed")
        )
        view._search_start_x_spin.setValue(10)
        view._search_start_y_spin.setValue(20)
        view._search_end_x_spin.setValue(40)
        view._search_end_y_spin.setValue(35)
        seen_sizes: list[tuple[int, int]] = []

        class Engine:
            def recognize_png(self, png: bytes) -> str:
                with Image.open(BytesIO(png)) as crop:
                    seen_sizes.append(crop.size)
                return "scope text"

        class ImmediatePool:
            def start(self, task) -> None:
                task.run()

        view._ocr_engine = Engine()
        monkeypatch.setattr(
            "automadin.ui.utility.utility_view.QThreadPool.globalInstance",
            lambda: ImmediatePool(),
        )
        view._test_ocr_crop()

        assert seen_sizes == [(30, 15)]
        assert "scope text" in view._ocr_result.toPlainText()

        view.close()
        view.deleteLater()
        db.close()
