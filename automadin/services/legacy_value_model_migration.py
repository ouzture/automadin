"""One-time conversion from the retired value model to version 1."""

from __future__ import annotations

import copy
from collections.abc import Iterable

from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    AutomationFlow,
    FlowCommand,
)
from automadin.models.run_flow_binding import RunFlowBinding


class LegacyValueModelMigrationError(ValueError):
    """A legacy record cannot be converted without guessing its behavior."""


def migrate_legacy_flows(flows: Iterable[AutomationFlow]) -> list[AutomationFlow]:
    """Convert a complete flow set, rejecting unsupported legacy data up front."""
    original = list(flows)
    by_name = {flow.name: flow for flow in original}
    if len(by_name) != len(original):
        raise LegacyValueModelMigrationError("Flow names must be unique for migration.")

    migrated: list[AutomationFlow] = []
    for flow in original:
        if flow.value_model_version == CURRENT_FLOW_VALUE_MODEL_VERSION:
            migrated.append(flow)
            continue
        if flow.parameters:
            raise LegacyValueModelMigrationError(
                f"Flow '{flow.name}' has legacy Parameters and cannot be converted automatically."
            )
        commands = copy.deepcopy(flow.commands)
        _migrate_commands(commands, by_name, flow.name)
        migrated.append(
            AutomationFlow(
                flow.name,
                commands,
                flow_id=flow.flow_id,
                value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
                resolution_scaling=flow.resolution_scaling,
            )
        )
    return migrated


def _migrate_commands(
    commands: list[FlowCommand],
    flows_by_name: dict[str, AutomationFlow],
    parent_name: str,
) -> None:
    for command in commands:
        if command.id == "run_flow":
            _migrate_run_flow(command, flows_by_name, parent_name)
        elif command.id == "input_fill" and command.params.get("value") == "{{item}}":
            command.params["value"] = {
                "kind": "template",
                "segments": [{"kind": "builtin", "name": "item"}],
            }
        _migrate_commands(command.children, flows_by_name, parent_name)
        _migrate_commands(command.else_children, flows_by_name, parent_name)


def _migrate_run_flow(
    command: FlowCommand,
    flows_by_name: dict[str, AutomationFlow],
    parent_name: str,
) -> None:
    target_name = command.params.get("flow_name")
    if not isinstance(target_name, str) or not target_name:
        raise LegacyValueModelMigrationError(
            f"Flow '{parent_name}' has a dynamic or missing Run Flow target."
        )
    child = flows_by_name.get(target_name)
    if child is None:
        raise LegacyValueModelMigrationError(
            f"Flow '{parent_name}' calls missing flow '{target_name}'."
        )
    if child.parameters:
        raise LegacyValueModelMigrationError(
            f"Flow '{parent_name}' calls '{target_name}', which has legacy Parameters."
        )
    global_params = {
        name: value
        for name, value in command.params.items()
        if name in {"pause_ms", "min_wait_time"}
    }
    command.params = {
        **RunFlowBinding(child.flow_id, {}, {}).to_params(),
        **global_params,
    }


__all__ = ["LegacyValueModelMigrationError", "migrate_legacy_flows"]
