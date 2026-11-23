from automadin.models.flow import CURRENT_FLOW_VALUE_MODEL_VERSION, AutomationFlow, FlowCommand
from automadin.models.run_flow_binding import RunFlowBinding
from automadin.services.database import DatabaseService


def test_database_startup_migrates_compatible_legacy_flows_atomically(tmp_path) -> None:
    database_path = tmp_path / "automadin.db"
    database = DatabaseService(str(database_path))
    child = AutomationFlow("Child")
    parent = AutomationFlow(
        "Parent",
        [
            FlowCommand("run_flow", params={"flow_name": "Child"}),
            FlowCommand(
                "for_each",
                params={"values": ["one"]},
                children=[FlowCommand("input_fill", params={"value": "{{item}}"})],
            ),
        ],
    )
    database.save_flow(child.name, child)
    database.save_flow(parent.name, parent)
    database.close()

    migrated_database = DatabaseService(str(database_path))
    migrated_child = migrated_database.load_flow("Child")
    migrated_parent = migrated_database.load_flow("Parent")
    assert migrated_child is not None
    assert migrated_parent is not None
    assert migrated_child.value_model_version == CURRENT_FLOW_VALUE_MODEL_VERSION
    assert migrated_parent.value_model_version == CURRENT_FLOW_VALUE_MODEL_VERSION
    assert RunFlowBinding.from_params(migrated_parent.commands[0].params).flow_id == migrated_child.flow_id
    assert migrated_parent.commands[1].children[0].params["value"] == {
        "kind": "template",
        "segments": [{"kind": "builtin", "name": "item"}],
    }
    migrated_database.close()
