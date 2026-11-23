"""Management tab — centralized list/rename/delete/import/export for all entities."""

from __future__ import annotations

import json

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from automadin.models.flow import AutomationFlow
from automadin.models.resource_path import (
    find_resource_collision,
    join_resource_path,
    normalize_resource_path,
    resource_group_path,
    resource_leaf_name,
    safe_export_stem,
)
from automadin.models.run_config import SavedRunConfig
from automadin.services.database import DatabaseService
from automadin.services.flow_preflight import PreflightMode, preflight_flow
from automadin.services.image_files import normalize_image_bytes_to_png
from automadin.ui.capture_controller import CaptureOperationController
from automadin.ui.management.entity_list_widget import EntityListWidget
from automadin.ui.management.folder_management_dialog import FolderManagementDialog
from automadin.ui.management.image_list_widget import ImageListWidget
from automadin.ui.preflight_dialog import confirm_document_preflight
from automadin.ui.resource_path_dialog import ResourcePathDialog
from automadin.ui.theme import COLORS
from automadin.ui.visual_rule_dialog import VisualRuleDialog


class ManagementView(QWidget):
    """Tab composing Flows, Run Configs, and Images management panels."""

    flow_renamed = Signal(str, str)   # old_name, new_name
    flow_deleted = Signal(str)        # name
    resource_renamed = Signal(str, str, str)  # kind, old_name, new_name
    resources_renamed = Signal(object)  # {kind: [(old_name, new_name), ...]}
    resource_deleted = Signal(str, str)  # kind, name
    run_config_changed = Signal()

    def __init__(
        self,
        db: DatabaseService,
        session_service=None,
        device_selector=None,
        parent: QWidget | None = None,
        *,
        capture_controller: CaptureOperationController | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._session_service = session_service
        self._device_selector = device_selector
        self._capture_controller = capture_controller
        self._build_ui()

    def _build_ui(self) -> None:
        tabs = QTabWidget(self)
        tabs.setDocumentMode(True)

        # ── Flows panel ──────────────────────────────────────────
        def rename_flow(old: str, new: str) -> None:
            if not self._db.rename_flow(old, new):
                raise ValueError(f"Flow '{old}' not found")
            self.flow_renamed.emit(old, new)
            self.resource_renamed.emit("flow", old, new)

        def delete_flow(name: str) -> bool:
            references = self._db.flow_references(name)
            message = f"Delete flow '{name}'? This cannot be undone."
            if references["flows"] or references["run_configs"]:
                details: list[str] = []
                if references["flows"]:
                    details.append("Sub-flow callers:\n  • " + "\n  • ".join(references["flows"]))
                if references["run_configs"]:
                    details.append(
                        "Saved run setups:\n  • " + "\n  • ".join(references["run_configs"])
                    )
                message = (
                    f"'{name}' is still referenced. Deleting it will leave broken links.\n\n"
                    + "\n\n".join(details)
                    + "\n\nDelete anyway?"
                )
            reply = QMessageBox.question(
                self,
                "Delete Flow",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return False
            result = self._db.delete_flow(name, force=True)
            self.flow_deleted.emit(name)
            if result:
                self.resource_deleted.emit("flow", name)
            return result

        def import_flow() -> None:
            path, _ = QFileDialog.getOpenFileName(
                self, "Import Flow", "", "JSON Files (*.json)"
            )
            if not path:
                return
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                flow = AutomationFlow.from_dict(data)
            except Exception as exc:
                QMessageBox.critical(self, "Import Error", str(exc))
                return
            destination = ResourcePathDialog.get_path(
                title="Import Flow",
                item_label="Flow",
                current_path=flow.name,
                ignore_path="",
                allow_existing=True,
                known_paths=self._all_resource_paths(),
                known_folders=self._all_resource_folders(),
                existing_paths=self._db.list_flows(),
                parent=self,
            )
            if destination is None:
                return
            collision = find_resource_collision(destination, self._db.list_flows())
            flow.name = collision or destination
            if collision is not None:
                reply = QMessageBox.question(
                    self, "Overwrite?",
                    f"A flow named '{collision}' already exists. Overwrite?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
            report = preflight_flow(
                flow,
                db=self._db,
                root_name=flow.name,
                mode=PreflightMode.DOCUMENT,
            )
            if not confirm_document_preflight(
                self,
                report,
                title="Import Flow Preflight",
            ):
                return
            self._db.save_flow(flow.name, flow)

        def export_flow(name: str) -> None:
            flow = self._db.load_flow(name)
            if flow is None:
                QMessageBox.warning(self, "Error", f"Could not load '{name}'.")
                return
            path, _ = QFileDialog.getSaveFileName(
                self,
                "Export Flow",
                f"{safe_export_stem(name)}.json",
                "JSON Files (*.json)",
            )
            if not path:
                return
            with open(path, "w", encoding="utf-8") as f:
                json.dump(flow.to_dict(), f, indent=2)

        self._flows_panel = EntityListWidget(
            title="Flows",
            list_fn=self._db.list_flows,
            delete_fn=delete_flow,
            rename_fn=rename_flow,
            import_fn=import_flow,
            export_fn=export_flow,
            known_paths_fn=self._all_resource_paths,
            known_folders_fn=self._all_resource_folders,
            detail_fn=self._flow_usage_text,
            item_label="Flow",
            confirm_delete=False,
        )
        tabs.addTab(self._flows_panel, "Flows")

        # ── Run Configs panel ─────────────────────────────────────
        def rename_run_config(old: str, new: str) -> None:
            config = self._db.load_run_config(old)
            if config is None:
                raise ValueError(f"Run setup '{old}' not found")
            new_name = resource_leaf_name(new)
            if not new_name:
                raise ValueError("Run setup name is required.")
            config.name = new_name
            config.group_path = resource_group_path(new)
            if not self._db.update_run_config(old, config):
                raise ValueError(f"Run setup '{old}' not found")
            self.run_config_changed.emit()

        def run_config_path(name: str) -> str:
            config = self._db.load_run_config(name)
            if config is None:
                return name
            return normalize_resource_path(
                f"{config.group_path}/{config.name}"
                if config.group_path
                else config.name
            )

        def delete_run_config(name: str) -> bool:
            schedules = self._db.run_config_references(name)
            if schedules:
                QMessageBox.warning(
                    self,
                    "Run Setup Is In Use",
                    f"'{name}' is used by {len(schedules)} schedule(s):\n\n  • "
                    + "\n  • ".join(schedules)
                    + "\n\nRemove or retarget those schedules before deleting this run setup.",
                )
                return False
            result = self._db.delete_run_config(name)
            self.run_config_changed.emit()
            return result

        def edit_run_config(name: str) -> None:
            from automadin.ui.launcher.run_config_dialog import RunConfigDialog
            cfg = self._db.load_run_config(name)
            dlg = RunConfigDialog(
                self._db,
                existing=cfg,
                session_service=self._session_service,
                parent=self,
            )
            if dlg.exec():
                saved = dlg.get_saved_run_config()
                if not self._db.update_run_config(name, saved):
                    QMessageBox.warning(self, "Run Setup Missing", "The run setup no longer exists.")
                    return
                self.run_config_changed.emit()

        def import_run_config() -> None:
            path, _ = QFileDialog.getOpenFileName(
                self, "Import Run Setup", "", "JSON Files (*.json)"
            )
            if not path:
                return
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cfg = SavedRunConfig.from_dict(data)
            except Exception as exc:
                QMessageBox.critical(self, "Import Error", str(exc))
                return
            try:
                join_resource_path(cfg.group_path, cfg.name)
            except ValueError as exc:
                QMessageBox.warning(self, "Invalid Run Setup", str(exc))
                return
            collision = find_resource_collision(cfg.name, self._db.list_run_configs())
            if collision is not None:
                reply = QMessageBox.question(
                    self, "Overwrite?",
                    f"A run setup named '{collision}' already exists. Overwrite?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
                cfg.name = collision
                self._db.update_run_config(collision, cfg)
            else:
                self._db.save_run_config(cfg.name, cfg)
            self.run_config_changed.emit()

        def export_run_config(name: str) -> None:
            cfg = self._db.load_run_config(name)
            if cfg is None:
                QMessageBox.warning(self, "Error", f"Could not load '{name}'.")
                return
            path, _ = QFileDialog.getSaveFileName(
                self,
                "Export Run Setup",
                f"{safe_export_stem(run_config_path(name))}.json",
                "JSON Files (*.json)",
            )
            if not path:
                return
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg.to_dict(), f, indent=2)

        self._runs_panel = EntityListWidget(
            title="Run setups",
            list_fn=self._db.list_run_configs,
            delete_fn=delete_run_config,
            rename_fn=rename_run_config,
            edit_fn=edit_run_config,
            import_fn=import_run_config,
            export_fn=export_run_config,
            path_fn=run_config_path,
            known_paths_fn=self._all_resource_paths,
            known_folders_fn=self._all_resource_folders,
            detail_fn=self._run_setup_usage_text,
            item_label="Run setup",
        )
        tabs.addTab(self._runs_panel, "Run setups")

        # ── Visual Rules panel ─────────────────────────────────────
        def new_visual_rule() -> None:
            dlg = VisualRuleDialog(
                session_service=self._session_service,
                device_selector=self._device_selector,
                known_paths=self._all_resource_paths(),
                known_folders=self._all_resource_folders(),
                capture_controller=self._capture_controller,
                parent=self,
            )
            accepted = False
            rule = None
            try:
                accepted = bool(dlg.exec())
                if accepted:
                    rule = dlg.get_rule()
            finally:
                dlg.deleteLater()
            if not accepted or rule is None:
                return
            if not rule.name:
                QMessageBox.warning(self, "Name Required", "Enter a visual rule name.")
                return
            collision = find_resource_collision(rule.name, self._db.list_visual_rules())
            if collision is not None:
                QMessageBox.warning(self, "Name Taken", f"'{collision}' already exists.")
                return
            self._db.save_visual_rule(rule)

        def edit_visual_rule(name: str) -> None:
            rule = self._db.load_visual_rule(name)
            if rule is None:
                QMessageBox.warning(self, "Error", f"Could not load '{name}'.")
                return
            dlg = VisualRuleDialog(
                existing=rule,
                session_service=self._session_service,
                device_selector=self._device_selector,
                known_paths=self._all_resource_paths(),
                known_folders=self._all_resource_folders(),
                capture_controller=self._capture_controller,
                parent=self,
            )
            accepted = False
            updated = None
            try:
                accepted = bool(dlg.exec())
                if accepted:
                    updated = dlg.get_rule()
            finally:
                dlg.deleteLater()
            if not accepted or updated is None:
                return
            if not updated.name:
                QMessageBox.warning(self, "Name Required", "Enter a visual rule name.")
                return
            collision = find_resource_collision(
                updated.name,
                self._db.list_visual_rules(),
                ignore_path=name,
            )
            if collision is not None:
                QMessageBox.warning(self, "Name Taken", f"'{collision}' already exists.")
                return
            renamed = updated.name != name
            if renamed:
                self._db.rename_visual_rule(name, updated.name)
            self._db.save_visual_rule(updated)
            if renamed:
                self.resource_renamed.emit("visual_rule", name, updated.name)

        def rename_visual_rule(old: str, new: str) -> None:
            if not self._db.rename_visual_rule(old, new):
                raise ValueError(f"Visual rule '{old}' not found")
            self.resource_renamed.emit("visual_rule", old, new)

        def delete_visual_rule(name: str) -> bool:
            affected = self._db.visual_rule_references(name)
            if affected:
                flow_list = "\n".join(f"  • {flow}" for flow in affected)
                message = (
                    f"'{name}' is used in {len(affected)} flow(s):\n\n{flow_list}\n\n"
                    "Deleting will leave broken visual-rule references. Delete anyway?"
                )
            else:
                message = f"Delete visual rule '{name}'? This cannot be undone."
            reply = QMessageBox.question(
                self,
                "Delete Visual Rule",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return False
            result = self._db.delete_visual_rule(name)
            if result:
                self.resource_deleted.emit("visual_rule", name)
            return result

        def export_visual_rule(name: str) -> None:
            rule = self._db.load_visual_rule(name)
            if rule is None:
                QMessageBox.warning(self, "Error", f"Could not load '{name}'.")
                return
            path, _ = QFileDialog.getSaveFileName(
                self,
                "Export Visual Rule",
                f"{safe_export_stem(name)}.json",
                "JSON Files (*.json)",
            )
            if not path:
                return
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rule.to_dict(), f, indent=2)

        self._visual_rules_panel = EntityListWidget(
            title="Visual Rules",
            list_fn=self._db.list_visual_rules,
            delete_fn=delete_visual_rule,
            rename_fn=rename_visual_rule,
            edit_fn=edit_visual_rule,
            import_fn=new_visual_rule,
            import_label="+ New",
            export_fn=export_visual_rule,
            known_paths_fn=self._all_resource_paths,
            known_folders_fn=self._all_resource_folders,
            detail_fn=self._visual_rule_usage_text,
            item_label="Visual rule",
            confirm_delete=False,
        )
        tabs.addTab(self._visual_rules_panel, "Visual rules")

        # ── Images panel ──────────────────────────────────────────
        def import_image() -> None:
            path, _ = QFileDialog.getOpenFileName(
                self, "Import Image", "",
                "Images (*.png *.jpg *.jpeg *.bmp *.tiff)"
            )
            if not path:
                return
            default_name = path.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
            name = ResourcePathDialog.get_path(
                title="Import Image",
                item_label="Image",
                current_path=default_name,
                ignore_path="",
                known_paths=self._all_resource_paths(),
                known_folders=self._all_resource_folders(),
                existing_paths=[item["name"] for item in self._db.list_images()],
                parent=self,
            )
            if name is None:
                return
            try:
                with open(path, "rb") as f:
                    image_bytes, width, height = normalize_image_bytes_to_png(f.read())
            except Exception as exc:
                QMessageBox.critical(self, "Import Error", str(exc))
                return
            self._db.save_image(name, image_bytes, width, height)

        def export_image(name: str) -> None:
            data = self._db.load_image(name)
            if data is None:
                QMessageBox.warning(self, "Error", f"Could not load image '{name}'.")
                return
            path, _ = QFileDialog.getSaveFileName(
                self,
                "Export Image",
                f"{safe_export_stem(name)}.png",
                "PNG (*.png)",
            )
            if not path:
                return
            with open(path, "wb") as f:
                f.write(data)

        self._images_panel = ImageListWidget(
            db=self._db,
            import_fn=import_image,
            export_fn=export_image,
            known_paths_fn=self._all_resource_paths,
            known_folders_fn=self._all_resource_folders,
        )
        self._images_panel.image_renamed.connect(
            lambda old, new: self.resource_renamed.emit("image", old, new)
        )
        self._images_panel.image_deleted.connect(
            lambda name: self.resource_deleted.emit("image", name)
        )
        tabs.addTab(self._images_panel, "Images")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(8)
        title_row = QHBoxLayout()
        title = QLabel("Automation library")
        title.setStyleSheet(
            f"color: {COLORS['text_primary']}; font-size: 16pt; font-weight: 700;"
        )
        title_row.addWidget(title)
        title_row.addStretch()
        folders_button = QPushButton("Manage folders…")
        folders_button.clicked.connect(self._manage_folders)
        title_row.addWidget(folders_button)
        layout.addLayout(title_row)
        subtitle = QLabel(
            "Browse, move, import, and export reusable automation resources."
        )
        subtitle.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 10pt;")
        layout.addWidget(subtitle)
        layout.addWidget(tabs, 1)

    def _all_resource_paths(self) -> list[str]:
        return self._db.list_resource_paths()

    def _all_resource_folders(self) -> list[str]:
        return self._db.list_resource_folders()

    def _flow_usage_text(self, name: str) -> str:
        references = self._db.flow_references(name)
        parts: list[str] = []
        if references["flows"]:
            parts.append("flows: " + ", ".join(references["flows"]))
        if references["run_configs"]:
            parts.append("run setups: " + ", ".join(references["run_configs"]))
        return "Used by " + "; ".join(parts) if parts else ""

    def _run_setup_usage_text(self, name: str) -> str:
        schedules = self._db.run_config_references(name)
        return (
            "Used by schedules: " + ", ".join(schedules)
            if schedules
            else ""
        )

    def _visual_rule_usage_text(self, name: str) -> str:
        flows = self._db.visual_rule_references(name)
        return "Used by flows: " + ", ".join(flows) if flows else ""

    def _manage_folders(self) -> None:
        dialog = FolderManagementDialog(self._db, self)
        dialog.exec()
        combined: dict[str, list[tuple[str, str]]] = {
            "flow": [],
            "image": [],
            "visual_rule": [],
            "run_config": [],
        }
        for batch in dialog.changes:
            for kind, changes in batch.items():
                combined[kind].extend(changes)
        if dialog.changes:
            self.resources_renamed.emit(combined)
            if combined["run_config"]:
                self.run_config_changed.emit()
            self._flows_panel.refresh()
            self._runs_panel.refresh()
            self._visual_rules_panel.refresh()
            self._images_panel.refresh()

    def refresh_run_list(self) -> None:
        """Called by MainWindow when run configs change from another tab."""
        self._runs_panel.refresh()

    def refresh_resource_list(self, kind: str) -> None:
        """Refresh one library after the resource changed elsewhere in the app."""
        panel = {
            "flow": self._flows_panel,
            "image": self._images_panel,
            "visual_rule": self._visual_rules_panel,
        }.get(kind)
        if panel is not None:
            panel.refresh()
