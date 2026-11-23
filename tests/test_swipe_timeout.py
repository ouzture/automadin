from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from automadin.engine.device_controller import AndroidDeviceController
from automadin.execution.context import FlowContext
from automadin.execution.flow_values import FlowInvocationValues
from automadin.models.device import AndroidDevice, DeviceSession
from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    AutomationFlow,
    FlowCommand,
)
from automadin.models.flow_value import FlowVariableDefinition
from automadin.models.typed_value import ValueType

from automadin.execution.handlers.actions import handle_swipe
from automadin.subprocess_utils import HIDDEN_PROCESS_CREATION_FLAGS


def _android_context(controller: object | None = None) -> FlowContext:
    data: dict[str, object] = {
        "adb_data": {"adb_path": "adb", "device_id": "emulator-5554"}
    }
    if controller is not None:
        data["device_controller"] = controller
    session = DeviceSession(
        device=AndroidDevice(device_id="emulator-5554"),
        data=data,
    )
    return FlowContext(session)


class SwipeTimeoutTests(unittest.TestCase):
    def test_android_swipe_timeout_extends_past_requested_duration(self) -> None:
        cmd = FlowCommand(
            "swipe",
            params={
                "coordinate_unit": "pixels",
                "start_x": 10,
                "start_y": 20,
                "end_x": 30,
                "end_y": 40,
                "duration_ms": 12000,
            },
        )

        run = Mock()
        controller = AndroidDeviceController(
            Mock(),
            {"adb_path": "adb", "device_id": "emulator-5554"},
            run_command=run,
        )
        with patch(
            "automadin.execution.handlers.actions.get_surface_size",
            return_value=(100, 100),
        ):
            handle_swipe(cmd, _android_context(controller))

        run.assert_called_once_with(
            [
                "adb",
                "-s",
                "emulator-5554",
                "shell",
                "input",
                "swipe",
                "10",
                "20",
                "30",
                "40",
                "12000",
            ],
            timeout=17,
            creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
        )

    def test_swipe_accepts_a_duration_variable_reference(self) -> None:
        variable_id = "30000000-0000-4000-8000-000000000008"
        flow = AutomationFlow(
            "Typed swipe",
            [],
            value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
            variables=[
                FlowVariableDefinition(
                    "remaining_time",
                    ValueType.DURATION_MS,
                    195_000,
                    id=variable_id,
                )
            ],
        )
        run = Mock()
        controller = AndroidDeviceController(
            Mock(),
            {"adb_path": "adb", "device_id": "emulator-5554"},
            run_command=run,
        )
        context = _android_context(controller)
        context.flow_values = FlowInvocationValues(
            flow.flow_id,
            flow.value_catalog,
            {},
        )
        command = FlowCommand(
            "swipe",
            params={
                "coordinate_unit": "pixels",
                "start_x": 10,
                "start_y": 20,
                "end_x": 30,
                "end_y": 40,
                "duration_ms": {"kind": "variable", "id": variable_id},
            },
        )

        with patch(
            "automadin.execution.handlers.actions.get_surface_size",
            return_value=(100, 100),
        ):
            handle_swipe(command, context)

        run.assert_called_once_with(
            [
                "adb",
                "-s",
                "emulator-5554",
                "shell",
                "input",
                "swipe",
                "10",
                "20",
                "30",
                "40",
                "195000",
            ],
            timeout=200,
            creationflags=HIDDEN_PROCESS_CREATION_FLAGS,
        )


if __name__ == "__main__":
    unittest.main()
