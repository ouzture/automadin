"""Handlers for declared version-1 flow Variables."""

from __future__ import annotations

from automadin.execution.context import FlowContext
from automadin.execution.flow_values import FlowRuntimeValueError
from automadin.models.flow import FlowCommand
from automadin.models.typed_value import canonical_typed_value_text
from automadin.models.variable_update import VariableUpdate, VariableUpdateError

_NON_CONTRACT_PARAMS = frozenset({"pause_ms", "min_wait_time", "log_values"})


def handle_update_variable(command: FlowCommand, context: FlowContext) -> None:
    if context.flow_values is None:
        raise RuntimeError("Update Variable is available only in version-1 flows.")
    contract_params = {
        name: value
        for name, value in command.params.items()
        if name not in _NON_CONTRACT_PARAMS
    }
    try:
        update = VariableUpdate.from_params(contract_params)
        result = context.flow_values.apply_variable_update(update)
    except (VariableUpdateError, FlowRuntimeValueError) as exc:
        raise RuntimeError(f"update_variable: {exc}") from exc
    if context.listener:
        target = context.flow_values.catalog.variable_by_id(update.target_variable_id)
        target_name = target.name if target is not None else update.target_variable_id
        detail = (
            f" = {canonical_typed_value_text(result)}"
            if command.params.get("log_values", False) is True
            else "; value hidden"
        )
        context.listener.on_log(
            f"Updated Variable '{target_name}' ({update.operation.value}){detail}"
        )


__all__ = ["handle_update_variable"]
