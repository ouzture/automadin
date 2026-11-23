from __future__ import annotations

import uuid

import pytest

from automadin.models.flow_value import (
    FlowInputDefinition,
    FlowValueCatalog,
    FlowValueType,
    FlowVariableDefinition,
)
from automadin.models.value_reference import (
    BuiltinValueReference,
    FlowValueTemplate,
    InputValueReference,
    LiteralValueReference,
    TemplateTextSegment,
    ValueReferenceError,
    VariableValueReference,
    render_value_reference,
    render_value_template,
    resolve_value_reference_type,
    value_reference_from_dict,
)


def _catalog() -> tuple[FlowValueCatalog, FlowInputDefinition, FlowVariableDefinition]:
    input_definition = FlowInputDefinition("account", FlowValueType.TEXT)
    variable = FlowVariableDefinition("attempts", FlowValueType.INTEGER, 0)
    return (
        FlowValueCatalog.build([input_definition], [variable], []),
        input_definition,
        variable,
    )


@pytest.mark.parametrize(
    "reference",
    [
        LiteralValueReference(FlowValueType.BOOLEAN, False),
        InputValueReference(str(uuid.uuid4())),
        VariableValueReference(str(uuid.uuid4())),
        BuiltinValueReference("_match_x"),
    ],
)
def test_each_reference_kind_round_trips_deterministically(reference) -> None:
    assert value_reference_from_dict(reference.to_dict()) == reference


def test_literal_reference_coerces_and_preserves_falsey_values() -> None:
    zero = LiteralValueReference("integer", "0")
    disabled = LiteralValueReference("boolean", False)
    empty = LiteralValueReference("text", "")

    assert zero.value == 0
    assert disabled.value is False
    assert empty.value == ""


def test_reference_ids_are_canonicalized_without_name_fallback() -> None:
    raw_id = uuid.uuid4().hex.upper()

    assert InputValueReference(raw_id).id == str(uuid.UUID(raw_id))
    with pytest.raises(ValueReferenceError, match="UUID string"):
        VariableValueReference("account")


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "unknown", "id": str(uuid.uuid4())},
        {"kind": "input"},
        {"kind": "input", "id": str(uuid.uuid4()), "name": "legacy"},
        {"kind": "literal", "type": "integer", "value": "bad"},
        {"kind": "builtin", "name": "bad name"},
    ],
)
def test_malformed_or_legacy_reference_shapes_fail_closed(data: object) -> None:
    with pytest.raises(ValueReferenceError):
        value_reference_from_dict(data)


def test_template_round_trip_coalesces_text_and_preserves_reference_order() -> None:
    catalog, input_definition, variable = _catalog()
    template = FlowValueTemplate(
        (
            TemplateTextSegment("Hello "),
            TemplateTextSegment("user "),
            InputValueReference(input_definition.id),
            TemplateTextSegment(" attempt "),
            VariableValueReference(variable.id),
            BuiltinValueReference("index"),
        )
    )

    assert template.segments[0] == TemplateTextSegment("Hello user ")
    assert FlowValueTemplate.from_dict(template.to_dict()) == template
    assert render_value_template(template, catalog) == (
        "Hello user {{account}} attempt {{attempts}}{{index}}"
    )


def test_empty_template_is_distinct_and_round_trips() -> None:
    template = FlowValueTemplate(())

    assert template.to_dict() == {"kind": "template", "segments": []}
    assert FlowValueTemplate.from_dict(template.to_dict()) == template


def test_template_rejects_literal_segments_and_unknown_fields() -> None:
    with pytest.raises(ValueReferenceError, match="cannot contain literal"):
        FlowValueTemplate.from_dict(
            {
                "kind": "template",
                "segments": [
                    {"kind": "literal", "type": "text", "value": "hidden"}
                ],
            }
        )
    with pytest.raises(ValueReferenceError, match="invalid fields"):
        FlowValueTemplate.from_dict(
            {"kind": "template", "segments": [], "legacy": "{{name}}"}
        )


def test_rename_changes_display_without_changing_saved_reference() -> None:
    _catalog_before, input_definition, _variable = _catalog()
    reference = InputValueReference(input_definition.id)
    renamed = FlowInputDefinition(
        "customer",
        input_definition.type,
        id=input_definition.id,
    )
    catalog_after = FlowValueCatalog.build([renamed], [], [])

    assert reference.to_dict()["id"] == input_definition.id
    assert render_value_reference(reference, catalog_after) == "{{customer}}"


def test_stale_references_render_repair_marker_and_fail_type_resolution() -> None:
    catalog = FlowValueCatalog.build([], [], [])
    reference = VariableValueReference(str(uuid.uuid4()))

    assert "<missing Variable" in render_value_reference(reference, catalog)
    with pytest.raises(ValueReferenceError, match="is stale"):
        resolve_value_reference_type(reference, catalog)


def test_reference_type_resolution_defers_builtin_scope_to_caller() -> None:
    catalog, input_definition, variable = _catalog()

    assert (
        resolve_value_reference_type(InputValueReference(input_definition.id), catalog)
        is FlowValueType.TEXT
    )
    assert (
        resolve_value_reference_type(VariableValueReference(variable.id), catalog)
        is FlowValueType.INTEGER
    )
    assert (
        resolve_value_reference_type(
            BuiltinValueReference("index"),
            catalog,
            builtin_types={"index": FlowValueType.INTEGER},
        )
        is FlowValueType.INTEGER
    )
    with pytest.raises(ValueReferenceError, match="unavailable in this scope"):
        resolve_value_reference_type(BuiltinValueReference("item"), catalog)
