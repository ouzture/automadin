from __future__ import annotations

from automadin.execution.executor import FlowExecutor
from automadin.models.device import DeviceSession
from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    AutomationFlow,
)
from automadin.models.flow_value import FlowInputDefinition, FlowVariableDefinition
from automadin.models.typed_value import ValueType
from automadin.ui.flow_runner.runner_view import _build_debug_context


def test_flow_debugger_creates_version_one_invocation_context_from_defaults() -> None:
    input_definition = FlowInputDefinition("attempts", ValueType.INTEGER, 2)
    variable_definition = FlowVariableDefinition("completed", ValueType.BOOLEAN, False)
    flow = AutomationFlow(
        "Rewards complete",
        value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        inputs=[input_definition],
        variables=[variable_definition],
    )

    context = _build_debug_context(flow, DeviceSession())

    assert context.flow_values is not None
    assert context.flow_values.flow_id == flow.flow_id
    assert context.flow_values.inputs[input_definition.id] == 2
    assert context.get_flow_variable(variable_definition.id) is False
    FlowExecutor(enforce_preflight=False).execute(flow, context)


def test_flow_debugger_keeps_legacy_flows_without_invocation_values() -> None:
    context = _build_debug_context(AutomationFlow("Legacy"), DeviceSession())

    assert context.flow_values is None
