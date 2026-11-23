from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    LEGACY_FLOW_VALUE_MODEL_VERSION,
    AutomationFlow,
    FlowCommand,
)
from automadin.services.database import DatabaseSchemaError, DatabaseService
from automadin.models.flow_parameter import FlowParameterDefinition, FlowParameterType
from automadin.models.flow_value import (
    FlowInputDefinition,
    FlowOutputDefinition,
    FlowValueDeclarationError,
    FlowValueType,
    FlowVariableDefinition,
)


def test_version_one_flow_contract_round_trips_with_stable_identity() -> None:
    input_definition = FlowInputDefinition("account", FlowValueType.TEXT)
    variable = FlowVariableDefinition("result", FlowValueType.TEXT, "")
    output = FlowOutputDefinition("result", FlowValueType.TEXT, variable.id)
    flow = AutomationFlow(
        "Checkout",
        flow_id=uuid.uuid4().hex.upper(),
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        inputs=[input_definition],
        variables=[variable],
        outputs=[output],
    )

    saved = flow.to_dict()
    restored = AutomationFlow.from_dict(saved)

    assert flow.flow_id == str(uuid.UUID(flow.flow_id))
    assert restored == flow
    assert "parameters" not in saved
    assert saved["value_model_version"] == 1
    assert restored.value_catalog.output_by_id(output.id) == output


def test_legacy_flow_remains_readable_with_explicit_version_zero() -> None:
    flow = AutomationFlow.from_dict(
        {
            "name": "Legacy",
            "commands": [],
            "parameters": [
                {"name": "account", "type": "text", "default": "work"}
            ],
        }
    )

    assert flow.value_model_version == LEGACY_FLOW_VALUE_MODEL_VERSION
    assert uuid.UUID(flow.flow_id)
    assert flow.parameters == [
        FlowParameterDefinition("account", FlowParameterType.TEXT, "work")
    ]
    assert "inputs" not in flow.to_dict()


@pytest.mark.parametrize(
    "data",
    [
        {
            "name": "Mixed Legacy",
            "commands": [],
            "value_model_version": 0,
            "inputs": [],
        },
        {
            "name": "Mixed Current",
            "commands": [],
            "value_model_version": 1,
            "parameters": [],
            "inputs": [],
            "variables": [],
            "outputs": [],
        },
        {
            "name": "Incomplete Current",
            "commands": [],
            "value_model_version": 1,
            "inputs": [],
        },
    ],
)
def test_mixed_or_incomplete_value_models_fail_closed(data: object) -> None:
    with pytest.raises(FlowValueDeclarationError):
        AutomationFlow.from_dict(data)  # type: ignore[arg-type]


def test_flow_identity_is_validated_and_preserved_when_name_changes() -> None:
    flow = AutomationFlow("Before")
    identity = flow.flow_id

    flow.name = "After"

    assert flow.flow_id == identity
    with pytest.raises(FlowValueDeclarationError, match="Flow ID"):
        AutomationFlow("Invalid", flow_id="not-a-uuid")


def test_database_preserves_identity_and_rejects_accidental_replacement(
    tmp_path,
) -> None:
    database = DatabaseService(str(tmp_path / "identity.db"))
    original = AutomationFlow("Checkout")
    database.save_flow(original.name, original)
    try:
        loaded = database.load_flow(original.name)
        assert loaded is not None
        loaded.commands = [FlowCommand("wait")]
        database.save_flow(loaded.name, loaded)

        assert database.load_flow(original.name).flow_id == original.flow_id  # type: ignore[union-attr]
        with pytest.raises(ValueError, match="cannot change its stable identity"):
            database.save_flow("Checkout", AutomationFlow("Checkout"))
        with pytest.raises(ValueError, match="already belongs"):
            database.save_flow(
                "Checkout Copy",
                AutomationFlow("Checkout Copy", flow_id=original.flow_id),
            )
    finally:
        database.close()


def test_rename_and_history_keep_identity_while_recreated_name_starts_fresh(
    tmp_path,
) -> None:
    database = DatabaseService(str(tmp_path / "history.db"))
    flow = AutomationFlow("Before")
    database.save_flow(flow.name, flow)
    flow.commands = [FlowCommand("wait")]
    database.save_flow(flow.name, flow)

    try:
        assert database.rename_flow("Before", "After")
        renamed = database.load_flow("After")
        assert renamed is not None
        assert renamed.flow_id == flow.flow_id
        version_ids = [item["id"] for item in database.list_flow_versions("After")]
        assert version_ids
        assert {
            database.load_flow_version(version_id).flow_id  # type: ignore[union-attr]
            for version_id in version_ids
        } == {flow.flow_id}

        assert database.delete_flow("After", force=True)
        replacement = AutomationFlow("After")
        database.save_flow(replacement.name, replacement)
        assert replacement.flow_id != flow.flow_id
        assert database.list_flow_versions("After") == []
    finally:
        database.close()


def test_database_round_trips_current_value_model_contract(tmp_path) -> None:
    database = DatabaseService(str(tmp_path / "values.db"))
    input_definition = FlowInputDefinition("account", FlowValueType.TEXT)
    variable = FlowVariableDefinition("result", FlowValueType.TEXT, "")
    output = FlowOutputDefinition("result", FlowValueType.TEXT, variable.id)
    flow = AutomationFlow(
        "Current",
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        inputs=[input_definition],
        variables=[variable],
        outputs=[output],
    )
    database.save_flow(flow.name, flow)
    try:
        assert database.load_flow(flow.name) == flow
    finally:
        database.close()


def test_database_rejects_serialized_flow_name_mismatch(tmp_path) -> None:
    path = tmp_path / "corrupt-name.db"
    database = DatabaseService(str(path))
    database.save_flow("Expected", AutomationFlow("Expected"))
    database.close()
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT data FROM flows WHERE name = 'Expected'"
        ).fetchone()
        assert row is not None
        data = json.loads(row[0])
        data["name"] = "Different"
        connection.execute(
            "UPDATE flows SET data = ? WHERE name = 'Expected'",
            (json.dumps(data),),
        )
        connection.commit()

    with pytest.raises(DatabaseSchemaError, match="name does not match"):
        DatabaseService(str(path))


def test_database_requires_flow_version_identity_index(tmp_path) -> None:
    path = tmp_path / "missing-history-index.db"
    database = DatabaseService(str(path))
    database.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX idx_flow_versions_flow_id_saved")
        connection.commit()

    with pytest.raises(DatabaseSchemaError, match="flow-version identity index"):
        DatabaseService(str(path))
