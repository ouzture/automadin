from __future__ import annotations

import uuid

import pytest

from automadin.models.flow_value import (
    FlowInputDefinition,
    FlowOutputDefinition,
    FlowValueCatalog,
    FlowValueDeclarationError,
    FlowValueType,
    FlowVariableDefinition,
    validate_flow_value_declarations,
)


def test_required_and_optional_inputs_round_trip_without_null_sentinel() -> None:
    required = FlowInputDefinition("account", FlowValueType.TEXT)
    optional = FlowInputDefinition("enabled", FlowValueType.BOOLEAN, False)

    assert required.required is True
    assert "default" not in required.to_dict()
    assert FlowInputDefinition.from_dict(required.to_dict()) == required
    assert optional.required is False
    assert optional.default is False
    assert FlowInputDefinition.from_dict(optional.to_dict()) == optional


def test_declaration_ids_are_canonical_uuid_values_and_survive_round_trip() -> None:
    raw_id = uuid.uuid4().hex.upper()
    definition = FlowVariableDefinition(
        "attempts",
        FlowValueType.INTEGER,
        "3",
        id=raw_id,
    )

    assert definition.id == str(uuid.UUID(raw_id))
    assert definition.initial == 3
    assert FlowVariableDefinition.from_dict(definition.to_dict()) == definition


def test_output_contract_round_trips_source_variable_identity() -> None:
    variable = FlowVariableDefinition("result", FlowValueType.NUMBER, 0)
    output = FlowOutputDefinition(
        "total",
        FlowValueType.NUMBER,
        variable.id,
    )

    assert FlowOutputDefinition.from_dict(output.to_dict()) == output
    assert validate_flow_value_declarations([], [variable], [output]) == (
        (),
        (variable,),
        (output,),
    )


@pytest.mark.parametrize("default", ["", 0, False])
def test_falsey_typed_defaults_are_supplied_not_missing(default: object) -> None:
    value_type = (
        FlowValueType.TEXT
        if isinstance(default, str)
        else FlowValueType.BOOLEAN
        if isinstance(default, bool)
        else FlowValueType.INTEGER
    )

    definition = FlowInputDefinition("value", value_type, default)

    assert definition.required is False
    assert definition.default == default


def test_value_names_are_unique_case_insensitively_across_contract() -> None:
    input_definition = FlowInputDefinition("Account", FlowValueType.TEXT)
    variable = FlowVariableDefinition("account", FlowValueType.TEXT, "")

    with pytest.raises(FlowValueDeclarationError, match="declared more than once"):
        validate_flow_value_declarations([input_definition], [variable], [])


def test_output_may_share_its_source_variable_name() -> None:
    variable = FlowVariableDefinition("result", FlowValueType.TEXT, "")
    output = FlowOutputDefinition("result", FlowValueType.TEXT, variable.id)

    assert validate_flow_value_declarations([], [variable], [output]) == (
        (),
        (variable,),
        (output,),
    )


def test_catalog_centralizes_case_insensitive_name_and_id_lookup() -> None:
    input_definition = FlowInputDefinition("Account", FlowValueType.TEXT)
    variable = FlowVariableDefinition("result", FlowValueType.TEXT, "")
    output = FlowOutputDefinition("result", FlowValueType.TEXT, variable.id)
    catalog = FlowValueCatalog.build([input_definition], [variable], [output])

    assert catalog.readable_by_name("ACCOUNT") is input_definition
    assert catalog.readable_by_name("Result") is variable
    assert catalog.input_by_id(input_definition.id.upper()) is input_definition
    assert catalog.variable_by_id(variable.id) is variable
    assert catalog.output_by_name("RESULT") is output
    assert catalog.output_by_id(output.id) is output
    assert catalog.readable_by_name("missing") is None


def test_catalog_direct_construction_cannot_bypass_validation() -> None:
    shared_id = str(uuid.uuid4())
    first = FlowInputDefinition("first", FlowValueType.TEXT, id=shared_id)
    second = FlowVariableDefinition(
        "second",
        FlowValueType.TEXT,
        "",
        id=shared_id,
    )

    with pytest.raises(FlowValueDeclarationError, match="ID .* more than once"):
        FlowValueCatalog((first,), (second,), ())


def test_value_ids_are_unique_across_declaration_kinds() -> None:
    shared_id = str(uuid.uuid4())
    input_definition = FlowInputDefinition(
        "account",
        FlowValueType.TEXT,
        id=shared_id,
    )
    variable = FlowVariableDefinition(
        "result",
        FlowValueType.TEXT,
        "",
        id=shared_id,
    )

    with pytest.raises(FlowValueDeclarationError, match="ID .* more than once"):
        validate_flow_value_declarations([input_definition], [variable], [])


def test_output_requires_existing_source_variable_of_same_type() -> None:
    variable = FlowVariableDefinition("result", FlowValueType.TEXT, "")
    missing = FlowOutputDefinition("missing", FlowValueType.TEXT, str(uuid.uuid4()))
    mismatched = FlowOutputDefinition(
        "mismatched",
        FlowValueType.INTEGER,
        variable.id,
    )

    with pytest.raises(FlowValueDeclarationError, match="missing source Variable"):
        validate_flow_value_declarations([], [variable], [missing])
    with pytest.raises(FlowValueDeclarationError, match="type must match"):
        validate_flow_value_declarations([], [variable], [mismatched])


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: FlowInputDefinition("bad name", FlowValueType.TEXT),
            "Input names",
        ),
        (
            lambda: FlowVariableDefinition(
                "count",
                FlowValueType.INTEGER,
                "not-an-integer",
            ),
            "Variable 'count' requires an integer",
        ),
        (
            lambda: FlowInputDefinition(
                "account",
                FlowValueType.TEXT,
                id="not-a-uuid",
            ),
            "Input ID must be a UUID",
        ),
    ],
)
def test_invalid_declarations_fail_closed(factory, message: str) -> None:
    with pytest.raises(FlowValueDeclarationError, match=message):
        factory()
