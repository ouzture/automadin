from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from automadin.models.device import BrowserDeviceConfig, BrowserEngine, ScreenMode
from automadin.models.flow import AutomationFlow, FlowCommand
from automadin.models.run_config import BrowserRunMode, RunConfig, SavedRunConfig
from automadin.models.visual_rule import VisualRule
from automadin.services.database import (
    DatabaseService,
    ImageAlreadyExistsError,
    ImageRevisionConflictError,
)


class DatabaseServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db = DatabaseService(str(Path(self._temp_dir.name) / "test.db"))

    def tearDown(self) -> None:
        self.db.close()
        self._temp_dir.cleanup()

    def test_each_worker_thread_gets_a_distinct_connection(self) -> None:
        connection_ids: list[int] = []
        barrier = threading.Barrier(3)

        def worker(index: int) -> None:
            connection_ids.append(id(self.db._conn))
            barrier.wait()
            self.db.set_app_setting(f"worker-{index}", str(index))

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(len(set(connection_ids)), 2)
        self.assertEqual(self.db.get_app_setting("worker-0"), "0")
        self.assertEqual(self.db.get_app_setting("worker-1"), "1")

    def test_worker_can_release_only_its_current_thread_connection(self) -> None:
        released: list[bool] = []
        closed: list[bool] = []

        def worker() -> None:
            connection = self.db._conn
            self.db.list_profile_records()
            released.append(self.db.release_current_thread_connection())
            released.append(self.db.release_current_thread_connection())
            try:
                connection.execute("SELECT 1")
            except sqlite3.ProgrammingError:
                closed.append(True)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        self.assertEqual(released, [True, False])
        self.assertEqual(closed, [True])
        self.db.set_app_setting("main-still-open", "yes")
        self.assertEqual(self.db.get_app_setting("main-still-open"), "yes")

    def test_visual_rule_rename_updates_payload_and_removes_old_name(self) -> None:
        self.db.save_visual_rule(VisualRule(name="before"))
        self.db.save_flow(
            "flow",
            AutomationFlow(
                "flow", [FlowCommand("click_best_target", params={"target_rule": "before"})]
            ),
        )

        self.assertTrue(self.db.rename_visual_rule("before", "after"))

        self.assertIsNone(self.db.load_visual_rule("before"))
        renamed = self.db.load_visual_rule("after")
        self.assertIsNotNone(renamed)
        self.assertEqual(renamed.name, "after")
        self.assertEqual(
            self.db.load_flow("flow").commands[0].params["target_rule"], "after"
        )

    def test_run_config_rename_is_atomic_and_updates_payload(self) -> None:
        saved = SavedRunConfig(
            name="before",
            flow_name="flow",
            device_id="android:test",
            config=RunConfig(),
        )
        self.db.save_run_config(saved.name, saved)

        self.assertTrue(self.db.rename_run_config("before", "after"))

        self.assertIsNone(self.db.load_run_config("before"))
        renamed = self.db.load_run_config("after")
        self.assertIsNotNone(renamed)
        self.assertEqual(renamed.name, "after")

    def test_run_config_pin_state_round_trips_and_defaults_to_unpinned(self) -> None:
        pinned = SavedRunConfig(
            name="Pinned",
            flow_name="flow",
            device_id="android:test",
            is_pinned=True,
        )
        self.db.save_run_config(pinned.name, pinned)

        restored = self.db.load_run_config(pinned.name)

        self.assertIsNotNone(restored)
        self.assertTrue(restored.is_pinned)
        legacy_payload = pinned.to_dict()
        legacy_payload.pop("is_pinned")
        self.assertFalse(SavedRunConfig.from_dict(legacy_payload).is_pinned)

    def test_run_config_value_prompt_policy_round_trips_and_defaults_to_asking(self) -> None:
        config = SavedRunConfig(
            name="No prompts",
            flow_name="flow",
            device_id="android:test",
            ask_for_values=False,
        )
        self.db.save_run_config(config.name, config)

        restored = self.db.load_run_config(config.name)

        self.assertIsNotNone(restored)
        self.assertFalse(restored.ask_for_values)
        legacy_payload = config.to_dict()
        legacy_payload.pop("ask_for_values")
        self.assertTrue(SavedRunConfig.from_dict(legacy_payload).ask_for_values)

    def test_browser_run_mode_round_trips_and_legacy_payloads_default_to_launch(
        self,
    ) -> None:
        connected = SavedRunConfig(
            name="Connected",
            flow_name="flow",
            device_id="chromium-desktop",
            browser_run_mode=BrowserRunMode.CONNECTED,
        )
        self.db.save_run_config(connected.name, connected)

        restored = self.db.load_run_config(connected.name)

        self.assertIsNotNone(restored)
        self.assertIs(restored.browser_run_mode, BrowserRunMode.CONNECTED)
        legacy_payload = connected.to_dict()
        legacy_payload.pop("browser_run_mode")
        self.assertIs(
            SavedRunConfig.from_dict(legacy_payload).browser_run_mode,
            BrowserRunMode.LAUNCH,
        )

    def test_run_configs_referencing_profile_support_current_and_legacy_ids(self) -> None:
        profile_name = "work-profile-eu"
        current_id = BrowserDeviceConfig(
            engine=BrowserEngine.CHROMIUM,
            screen_mode=ScreenMode.DESKTOP,
            profile_name=profile_name,
        ).to_id()
        setups = (
            SavedRunConfig(
                name="Current nested run",
                group_path="Browser/Daily",
                flow_name="flow",
                device_id=current_id,
            ),
            SavedRunConfig(
                name="Legacy run",
                flow_name="flow",
                device_id=f"chromium|hl|desktop|perm|{profile_name.upper()}",
            ),
            SavedRunConfig(
                name="Other profile",
                flow_name="flow",
                device_id="chromium-desktop-other-profile",
            ),
            SavedRunConfig(
                name="Malformed legacy",
                flow_name="flow",
                device_id=f"chromium|hl|invalid-mode|perm|{profile_name}",
            ),
            SavedRunConfig(
                name="Missing legacy profile segment",
                flow_name="flow",
                device_id="chromium|hl|desktop|perm",
            ),
        )
        for setup in setups:
            self.db.save_run_config(setup.name, setup)

        references = self.db.list_run_configs_referencing_profile(profile_name)

        self.assertEqual(
            [(config.name, config.group_path) for config in references],
            [
                ("Current nested run", "Browser/Daily"),
                ("Legacy run", ""),
            ],
        )

    def test_run_setup_update_collision_keeps_both_existing_rows(self) -> None:
        for name in ("Morning", "Evening"):
            self.db.save_run_config(
                name,
                SavedRunConfig(
                    name=name,
                    flow_name="flow",
                    device_id="android:test",
                    config=RunConfig(),
                ),
            )
        updated = self.db.load_run_config("Morning")
        self.assertIsNotNone(updated)
        updated.name = "evening"

        with self.assertRaisesRegex(ValueError, "already exists"):
            self.db.update_run_config("Morning", updated)

        self.assertIsNotNone(self.db.load_run_config("Morning"))
        self.assertIsNotNone(self.db.load_run_config("Evening"))

    def test_flow_rename_updates_run_config_reference(self) -> None:
        self.db.save_flow("before", AutomationFlow("before"))
        self.db.save_run_config(
            "run",
            SavedRunConfig(
                name="run", flow_name="before", device_id="android:test", config=RunConfig()
            ),
        )

        self.assertTrue(self.db.rename_flow("before", "after"))

        self.assertEqual(self.db.load_run_config("run").flow_name, "after")

    def test_flow_move_updates_nested_run_flow_references_and_version_names(self) -> None:
        self.db.save_flow("Operations/Old", AutomationFlow("Operations/Old"))
        caller = AutomationFlow(
            "Operations/Caller",
            [
                FlowCommand(
                    "if_time_elapsed",
                    else_children=[
                        FlowCommand("run_flow", params={"flow_name": "Operations/Old"})
                    ],
                )
            ],
        )
        self.db.save_flow(caller.name, caller)
        updated = self.db.load_flow("Operations/Old")
        self.assertIsNotNone(updated)
        assert updated is not None
        updated.commands = [FlowCommand("wait")]
        self.db.save_flow("Operations/Old", updated)

        self.assertTrue(
            self.db.rename_flow("Operations/Old", "Operations/Archive/New")
        )

        updated_caller = self.db.load_flow("Operations/Caller")
        self.assertEqual(
            updated_caller.commands[0].else_children[0].params["flow_name"],
            "Operations/Archive/New",
        )
        versions = self.db.list_flow_versions("Operations/Archive/New")
        self.assertTrue(versions)
        for version in versions:
            restored = self.db.load_flow_version(version["id"])
            self.assertEqual(restored.name, "Operations/Archive/New")
        caller_versions = self.db.list_flow_versions("Operations/Caller")
        self.assertTrue(caller_versions)
        for version in caller_versions:
            restored = self.db.load_flow_version(version["id"])
            self.assertEqual(
                restored.commands[0].else_children[0].params["flow_name"],
                "Operations/Archive/New",
            )

    def test_referenced_flow_delete_requires_explicit_force(self) -> None:
        self.db.save_flow("Shared/Child", AutomationFlow("Shared/Child"))
        self.db.save_flow(
            "Shared/Caller",
            AutomationFlow(
                "Shared/Caller",
                [FlowCommand("run_flow", params={"flow_name": "Shared/Child"})],
            ),
        )
        self.db.save_run_config(
            "run child",
            SavedRunConfig(
                name="run child",
                flow_name="Shared/Child",
                device_id="android:test",
            ),
        )

        self.assertEqual(
            self.db.flow_references("Shared/Child"),
            {"flows": ["Shared/Caller"], "run_configs": ["run child"]},
        )
        with self.assertRaisesRegex(ValueError, "still referenced"):
            self.db.delete_flow("Shared/Child")
        self.assertIsNotNone(self.db.load_flow("Shared/Child"))
        self.assertTrue(self.db.delete_flow("Shared/Child", force=True))

    def test_stable_run_flow_reference_blocks_delete_and_reports_caller(self) -> None:
        child = AutomationFlow("Shared/Child")
        self.db.save_flow(child.name, child)
        self.db.save_flow(
            "Shared/Caller",
            AutomationFlow(
                "Shared/Caller",
                [
                    FlowCommand(
                        "run_flow",
                        params={
                            "flow_id": child.flow_id,
                            "input_mappings": {},
                            "output_mappings": {},
                        },
                    )
                ],
            ),
        )

        self.assertEqual(
            self.db.flow_references("Shared/Child")["flows"],
            ["Shared/Caller"],
        )
        with self.assertRaisesRegex(ValueError, "still referenced"):
            self.db.delete_flow("Shared/Child")

    def test_folder_move_is_atomic_across_resource_types_and_references(self) -> None:
        child = AutomationFlow("Work/Child")
        self.db.save_flow(child.name, child)
        self.db.save_flow(
            "Work/Caller",
            AutomationFlow(
                "Work/Caller",
                [
                    FlowCommand(
                        "run_flow",
                        params={
                            "flow_id": child.flow_id,
                            "input_mappings": {},
                            "output_mappings": {},
                        },
                    ),
                    FlowCommand("click_image", params={"image_name": "Work/Images/Button"}),
                    FlowCommand("smart_click", params={"target_rule": "Work/Rules/Target"}),
                ],
            ),
        )
        self.db.save_image("Work/Images/Button", b"image", 1, 1)
        self.db.save_visual_rule(VisualRule(name="Work/Rules/Target"))
        self.db.save_run_config(
            "Daily",
            SavedRunConfig(
                name="Daily",
                flow_name="Work/Child",
                device_id="android:test",
                group_path="Work/Runs",
            ),
        )

        changes = self.db.move_resource_folder("Work", "Archive")

        self.assertIn(("Work/Child", "Archive/Child"), changes["flow"])
        self.assertEqual(
            self.db.flow_references("Archive/Child")["flows"],
            ["Archive/Caller"],
        )
        self.assertEqual(
            self.db.image_references("Archive/Images/Button"),
            ["Archive/Caller"],
        )
        self.assertEqual(
            self.db.visual_rule_references("Archive/Rules/Target"),
            ["Archive/Caller"],
        )
        run = self.db.load_run_config("Daily")
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run.flow_name, "Archive/Child")
        self.assertEqual(run.group_path, "Archive/Runs")

        self.db.move_resource_folder("Archive/Runs", "Archive")
        self.assertEqual(self.db.load_run_config("Daily").group_path, "Archive")

    def test_named_resources_reject_normalized_case_collisions(self) -> None:
        self.db.save_flow("Games/Collect", AutomationFlow("Games/Collect"))
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.db.save_flow("games\\collect", AutomationFlow("games\\collect"))

    def test_workspace_folders_are_shared_across_resource_types(self) -> None:
        self.db.save_flow("Mobile/Daily/Login", AutomationFlow("Mobile/Daily/Login"))
        self.db.save_image("mobile/Shop/Button", b"image", 1, 1)

        self.assertEqual(
            self.db.list_resource_folders(),
            ["Mobile", "Mobile/Daily", "Mobile/Shop"],
        )

    def test_legacy_run_folder_variants_resolve_to_one_explicit_spelling(self) -> None:
        self.db.save_flow("iom/click_fire_bomb", AutomationFlow("iom/click_fire_bomb"))
        for index in range(8):
            self.db.save_image(f"iom/images/button-{index}", b"image", 1, 1)
            self.db.save_image(f"rsl/images/button-{index}", b"image", 1, 1)

        groups = {
            "Fire Bomb": "IOM",
            "Fire Bomb Slow": "IOM",
            "Google Play": "IOM",
            "Phone": "iom",
            "Rewards": "RSL",
        }
        for name, group in groups.items():
            config = SavedRunConfig(
                name=name,
                group_path=group,
                flow_name="iom/click_fire_bomb",
                device_id="android:test",
            )
            self.db.save_run_config(name, config)
            payload = config.to_dict()
            payload["group_path"] = group
            self.db._conn.execute(
                "UPDATE run_configs SET data = ? WHERE name = ?",
                (json.dumps(payload), name),
            )
        self.db._conn.commit()

        folders = self.db.list_resource_folders()
        self.assertIn("IOM", folders)
        self.assertIn("RSL", folders)
        self.assertNotIn("iom", folders)
        self.assertNotIn("rsl", folders)
        self.assertEqual(self.db.resolve_resource_folder("iom"), "IOM")

        phone = self.db.load_run_config("Phone")
        self.assertIsNotNone(phone)
        phone.group_path = "iom"
        self.assertTrue(self.db.update_run_config("Phone", phone))
        self.assertEqual(self.db.load_run_config("Phone").group_path, "IOM")

    def test_image_rename_updates_scalar_and_list_references(self) -> None:
        self.db.save_image("before", b"png")
        self.db.save_flow(
            "flow",
            AutomationFlow(
                "flow",
                [
                    FlowCommand("find_image", params={"image_name": "before"}),
                    FlowCommand("if_image", params={"images": ["one", "before"]}),
                ],
            ),
        )

        self.assertTrue(self.db.rename_image("before", "after"))

        flow = self.db.load_flow("flow")
        self.assertEqual(flow.commands[0].params["image_name"], "after")
        self.assertEqual(flow.commands[1].params["images"], ["one", "after"])
        for version in self.db.list_flow_versions("flow"):
            restored = self.db.load_flow_version(version["id"])
            self.assertNotIn("before", str(restored.to_dict()))

    def test_create_image_normalizes_name_and_returns_immutable_revision(self) -> None:
        revision = self.db.create_image(
            r"Mobile\Buttons\Collect",
            b"first image",
            12,
            34,
        )

        self.assertEqual(revision.name, "Mobile/Buttons/Collect")
        self.assertEqual((revision.width, revision.height), (12, 34))
        self.assertEqual(
            revision,
            self.db.image_revision("mobile/buttons/collect"),
        )
        with self.assertRaises((AttributeError, TypeError)):
            revision.width = 99

    def test_image_capture_surface_metadata_round_trips(self) -> None:
        revision = self.db.create_image(
            "Browser/Submit",
            b"image",
            20,
            10,
            source_surface_width=1280,
            source_surface_height=720,
            source_coordinate_space="browser_viewport",
        )

        asset = self.db.load_image_asset("Browser/Submit")

        self.assertIsNotNone(asset)
        self.assertEqual(asset.data, b"image")
        self.assertEqual(asset.source_surface_size, (1280, 720))
        self.assertEqual(asset.source_coordinate_space, "browser_viewport")
        self.assertEqual(revision.source_surface_width, 1280)

    def test_create_image_refuses_case_and_separator_collisions(self) -> None:
        self.db.create_image("Mobile/Buttons/Collect", b"first", 1, 2)

        with self.assertRaisesRegex(ImageAlreadyExistsError, "already exists"):
            self.db.create_image(r"mobile\buttons\collect", b"second", 3, 4)

        self.assertEqual(self.db.load_image("Mobile/Buttons/Collect"), b"first")

    def test_image_revision_resolves_name_before_loading_one_blob(self) -> None:
        self.db.create_image("Mobile/One", b"one" * 1000, 1, 1)
        self.db.create_image("Mobile/Two", b"two" * 1000, 2, 2)
        statements: list[str] = []
        self.db._conn.set_trace_callback(statements.append)
        try:
            revision = self.db.image_revision(r"mobile\two")
        finally:
            self.db._conn.set_trace_callback(None)

        self.assertIsNotNone(revision)
        blob_reads = [
            statement
            for statement in statements
            if "SELECT name, data, width, height" in statement
        ]
        self.assertEqual(len(blob_reads), 1)
        self.assertIn("WHERE name =", blob_reads[0])

    def test_replace_image_if_revision_succeeds_for_unchanged_exact_name(self) -> None:
        reviewed = self.db.create_image("Mobile/Collect", b"before", 10, 20)

        replaced = self.db.replace_image_if_revision(
            reviewed,
            b"after",
            30,
            40,
        )

        self.assertEqual(replaced.name, reviewed.name)
        self.assertNotEqual(replaced.sha256, reviewed.sha256)
        self.assertEqual((replaced.width, replaced.height), (30, 40))
        self.assertEqual(self.db.load_image(reviewed.name), b"after")

    def test_replace_image_rejects_changed_revision(self) -> None:
        reviewed = self.db.create_image("Mobile/Collect", b"before", 10, 20)
        self.db.save_image("Mobile/Collect", b"changed", 10, 20)

        with self.assertRaisesRegex(ImageRevisionConflictError, "changed"):
            self.db.replace_image_if_revision(reviewed, b"replacement", 30, 40)

        self.assertEqual(self.db.load_image("Mobile/Collect"), b"changed")

    def test_replace_image_rejects_changed_dimensions(self) -> None:
        reviewed = self.db.create_image("Mobile/Collect", b"same bytes", 10, 20)
        self.db.save_image("Mobile/Collect", b"same bytes", 11, 20)

        with self.assertRaisesRegex(ImageRevisionConflictError, "changed"):
            self.db.replace_image_if_revision(reviewed, b"replacement", 30, 40)

        current = self.db.image_revision("Mobile/Collect")
        self.assertIsNotNone(current)
        self.assertEqual((current.width, current.height), (11, 20))

    def test_replace_image_rejects_deleted_revision(self) -> None:
        reviewed = self.db.create_image("Mobile/Collect", b"before", 10, 20)
        self.assertTrue(self.db.delete_image(reviewed.name))

        with self.assertRaisesRegex(ImageRevisionConflictError, "no longer exists"):
            self.db.replace_image_if_revision(reviewed, b"replacement", 30, 40)

    def test_replace_image_rejects_renamed_revision(self) -> None:
        reviewed = self.db.create_image("Mobile/Collect", b"before", 10, 20)
        self.assertTrue(self.db.rename_image(reviewed.name, "Mobile/Collect Renamed"))

        with self.assertRaisesRegex(ImageRevisionConflictError, "no longer exists"):
            self.db.replace_image_if_revision(reviewed, b"replacement", 30, 40)

        self.assertEqual(self.db.load_image("Mobile/Collect Renamed"), b"before")

    def test_image_references_cover_nested_scalar_and_list_parameters(self) -> None:
        self.db.create_image("Mobile/Collect", b"image", 1, 1)
        self.db.save_flow(
            "Scalar caller",
            AutomationFlow(
                "Scalar caller",
                [
                    FlowCommand(
                        "if_time_elapsed",
                        children=[
                            FlowCommand(
                                "find_image",
                                params={"image_name": r"mobile\collect"},
                            )
                        ],
                    )
                ],
            ),
        )
        self.db.save_flow(
            "List caller",
            AutomationFlow(
                "List caller",
                [
                    FlowCommand(
                        "if_time_elapsed",
                        else_children=[
                            FlowCommand(
                                "if_image",
                                params={"images": ["other", "MOBILE/COLLECT"]},
                            )
                        ],
                    )
                ],
            ),
        )
        self.db.save_flow(
            "Unrelated",
            AutomationFlow(
                "Unrelated",
                [FlowCommand("if_image", params={"images": ["other"]})],
            ),
        )

        self.assertEqual(
            self.db.image_references("Mobile/Collect"),
            ["List caller", "Scalar caller"],
        )


if __name__ == "__main__":
    unittest.main()
