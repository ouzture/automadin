"""Context construction shared by interactive flow and single-step debugging."""

from __future__ import annotations

from automadin.execution.context import FlowContext
from automadin.execution.flow_values import FlowInvocationValues, resolve_flow_inputs
from automadin.models.flow import CURRENT_FLOW_VALUE_MODEL_VERSION, AutomationFlow


def build_debug_context(flow: AutomationFlow, session: object) -> FlowContext:
    """Create isolated runtime values for an interactively executed flow."""
    flow_values = None
    if flow.value_model_version == CURRENT_FLOW_VALUE_MODEL_VERSION:
        inputs = resolve_flow_inputs(flow.value_catalog.inputs)
        flow_values = FlowInvocationValues(
            flow.flow_id,
            flow.value_catalog,
            inputs,
        )
    return FlowContext(session, flow_values=flow_values)  # type: ignore[arg-type]


__all__ = ["build_debug_context"]
