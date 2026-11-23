from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from automadin.execution.context import FlowContext
from automadin.execution.flow_values import FlowInvocationValues, FlowRuntimeValueError
from automadin.execution.handlers.control import handle_if_image, handle_while
from automadin.execution.handlers.windows import handle_win_if_image, handle_win_while
from automadin.models.device import DeviceSession, WindowsDevice
from automadin.models.flow import (
    CURRENT_FLOW_VALUE_MODEL_VERSION,
    AutomationFlow,
    FlowCommand,
)
from automadin.models.typed_value import ValueType
from automadin.models.value_reference import BuiltinValueReference


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def _execute_children(
        self, commands: list[FlowCommand], _ctx: FlowContext
    ) -> None:
        self.calls.append([command.id for command in commands])


def _context() -> tuple[FlowContext, _RecordingExecutor]:
    ctx = FlowContext(DeviceSession())
    executor = _RecordingExecutor()
    ctx._executor = executor
    return ctx, executor


def _windows_context() -> tuple[FlowContext, _RecordingExecutor]:
    session = DeviceSession(
        device=WindowsDevice(hwnd=10, title="Game"),
        data={"windows_data": {"hwnd": 10, "title": "Game"}},
    )
    ctx = FlowContext(session)
    executor = _RecordingExecutor()
    ctx._executor = executor
    return ctx, executor


class IfImageTimeoutRuntimeTests(unittest.TestCase):
    def test_match_coordinates_are_scoped_to_then_children(self) -> None:
        command = FlowCommand(
            "if_image",
            params={"images": ["target"]},
            children=[FlowCommand("swipe")],
        )
        flow = AutomationFlow(
            "Scoped image coordinates",
            [command],
            value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        )
        values = FlowInvocationValues(flow.flow_id, flow.value_catalog, {})
        ctx = FlowContext(DeviceSession(), flow_values=values)
        resolved: list[tuple[int, int]] = []

        def execute_children(_commands, child_ctx: FlowContext) -> None:
            resolved.append(
                (
                    int(
                        child_ctx.resolve_command_scalar(
                            BuiltinValueReference("_match_x").to_dict(),
                            ValueType.NUMBER,
                        )
                    ),
                    int(
                        child_ctx.resolve_command_scalar(
                            BuiltinValueReference("_match_y").to_dict(),
                            ValueType.NUMBER,
                        )
                    ),
                )
            )

        ctx._executor = SimpleNamespace(_execute_children=execute_children)
        ctx.set_variable("_match_x", "321")
        ctx.set_variable("_match_y", "654")

        with patch(
            "automadin.execution.handlers.control._check_image_condition",
            return_value=True,
        ):
            handle_if_image(command, ctx)

        self.assertEqual(resolved, [(321, 654)])
        with self.assertRaisesRegex(FlowRuntimeValueError, "unavailable in this scope"):
            values.resolve(BuiltinValueReference("_match_x"))

    def test_not_found_children_do_not_receive_fake_match_coordinates(self) -> None:
        command = FlowCommand(
            "if_image",
            params={"images": ["target"], "condition": "not_found"},
            children=[FlowCommand("swipe")],
        )
        flow = AutomationFlow(
            "No fake coordinates",
            [command],
            value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        )
        values = FlowInvocationValues(flow.flow_id, flow.value_catalog, {})
        ctx = FlowContext(DeviceSession(), flow_values=values)
        ctx._executor = SimpleNamespace(
            _execute_children=lambda _commands, _ctx: values.resolve(
                BuiltinValueReference("_match_x")
            )
        )

        with patch(
            "automadin.execution.handlers.control._check_image_condition",
            return_value=True,
        ):
            with self.assertRaisesRegex(
                FlowRuntimeValueError, "unavailable in this scope"
            ):
                handle_if_image(command, ctx)

    def test_while_match_coordinates_refresh_for_each_iteration(self) -> None:
        command = FlowCommand(
            "while",
            params={"images": ["target"], "timeout": 1000},
            children=[FlowCommand("swipe")],
        )
        flow = AutomationFlow(
            "Moving image coordinates",
            [command],
            value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        )
        values = FlowInvocationValues(flow.flow_id, flow.value_catalog, {})
        ctx = FlowContext(DeviceSession(), flow_values=values)
        resolved: list[tuple[int, int]] = []

        def execute_children(_commands, child_ctx: FlowContext) -> None:
            resolved.append(
                (
                    int(
                        child_ctx.resolve_command_scalar(
                            BuiltinValueReference("_match_x").to_dict(),
                            ValueType.NUMBER,
                        )
                    ),
                    int(
                        child_ctx.resolve_command_scalar(
                            BuiltinValueReference("_match_y").to_dict(),
                            ValueType.NUMBER,
                        )
                    ),
                )
            )

        matches = iter(((10, 20), (30, 40), None))

        def check(*_args) -> bool:
            coordinates = next(matches)
            if coordinates is None:
                ctx.set_variable("_match_x", "0")
                ctx.set_variable("_match_y", "0")
                return False
            ctx.set_variable("_match_x", str(coordinates[0]))
            ctx.set_variable("_match_y", str(coordinates[1]))
            return True

        ctx._executor = SimpleNamespace(_execute_children=execute_children)
        with patch(
            "automadin.execution.handlers.control._check_image_condition",
            side_effect=check,
        ):
            handle_while(command, ctx)

        self.assertEqual(resolved, [(10, 20), (30, 40)])
        with self.assertRaisesRegex(FlowRuntimeValueError, "unavailable in this scope"):
            values.resolve(BuiltinValueReference("_match_x"))

    def test_zero_timeout_preserves_one_shot_else_behavior(self) -> None:
        ctx, executor = _context()
        cmd = FlowCommand(
            "if_image",
            params={"images": ["target"], "timeout": 0},
            children=[FlowCommand("then")],
            else_children=[FlowCommand("else")],
        )

        with patch(
            "automadin.execution.handlers.control._check_image_condition",
            return_value=False,
        ) as check:
            handle_if_image(cmd, ctx)

        check.assert_called_once()
        self.assertEqual(executor.calls, [["else"]])

    def test_attention_retry_rechecks_the_full_condition_before_branching(self) -> None:
        class Listener:
            requests = 0

            def on_attention_required(self, request) -> None:
                self.requests += 1
                request.resolve("retry")

            def on_log(self, _message: str) -> None:
                return None

        ctx, executor = _context()
        listener = Listener()
        ctx.listener = listener
        cmd = FlowCommand(
            "if_image",
            params={
                "images": ["target"],
                "condition": "not_found",
                "condition_unmet_behavior": "attention",
            },
            children=[FlowCommand("then")],
            else_children=[FlowCommand("else")],
        )

        with patch(
            "automadin.execution.handlers.control._check_image_condition",
            side_effect=[False, True],
        ) as check:
            handle_if_image(cmd, ctx)

        self.assertEqual(check.call_count, 2)
        self.assertEqual(listener.requests, 1)
        self.assertEqual(executor.calls, [["then"]])

    def test_positive_timeout_polls_until_found_and_clicks_once(self) -> None:
        ctx, executor = _context()
        cmd = FlowCommand(
            "if_image",
            params={
                "images": ["target"],
                "timeout": 100,
                "clickIfFound": True,
            },
            children=[FlowCommand("then")],
            else_children=[FlowCommand("else")],
        )

        with (
            patch(
                "automadin.execution.handlers.control.time.monotonic",
                side_effect=[10.0, 10.0, 10.01],
            ),
            patch("automadin.execution.handlers.control.time.sleep") as sleep,
            patch(
                "automadin.execution.handlers.control._check_image_condition",
                side_effect=[False, True],
            ) as check,
            patch(
                "automadin.execution.handlers.control._do_click_if_found"
            ) as click,
        ):
            handle_if_image(cmd, ctx)

        self.assertEqual(check.call_count, 2)
        sleep.assert_called_once_with(0.05)
        click.assert_called_once_with(ctx, 0, 0, 0)
        self.assertEqual(executor.calls, [["then"]])

    def test_positive_timeout_uses_remaining_budget_then_else_once(self) -> None:
        ctx, executor = _context()
        cmd = FlowCommand(
            "if_image",
            params={"images": ["target"], "timeout": 20},
            children=[FlowCommand("then")],
            else_children=[FlowCommand("else")],
        )

        with (
            patch(
                "automadin.execution.handlers.control.time.monotonic",
                side_effect=[2.0, 2.005, 2.020],
            ),
            patch("automadin.execution.handlers.control.time.sleep") as sleep,
            patch(
                "automadin.execution.handlers.control._check_image_condition",
                return_value=False,
            ),
        ):
            handle_if_image(cmd, ctx)

        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.015)
        self.assertEqual(executor.calls, [["else"]])

    def test_stop_during_wait_prevents_more_checks_and_branches(self) -> None:
        ctx, executor = _context()
        cmd = FlowCommand(
            "if_image",
            params={"images": ["target"], "timeout": 100},
            children=[FlowCommand("then")],
            else_children=[FlowCommand("else")],
        )

        with (
            patch(
                "automadin.execution.handlers.control.time.monotonic",
                side_effect=[4.0, 4.0],
            ),
            patch(
                "automadin.execution.handlers.control.time.sleep",
                side_effect=lambda _seconds: ctx.stop(),
            ),
            patch(
                "automadin.execution.handlers.control._check_image_condition",
                return_value=False,
            ) as check,
        ):
            handle_if_image(cmd, ctx)

        check.assert_called_once()
        self.assertEqual(executor.calls, [])


class WinIfImageTimeoutRuntimeTests(unittest.TestCase):
    def test_win_if_image_exposes_match_coordinates_to_children(self) -> None:
        result = SimpleNamespace(center_x=45, center_y=67, confidence=0.95)
        command = FlowCommand(
            "win_if_image",
            params={"images": ["target"]},
            children=[FlowCommand("win_click")],
        )
        flow = AutomationFlow(
            "Windows image coordinates",
            [command],
            value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        )
        values = FlowInvocationValues(flow.flow_id, flow.value_catalog, {})
        session = DeviceSession(
            device=WindowsDevice(hwnd=10, title="Game"),
            data={"windows_data": {"hwnd": 10, "title": "Game"}},
        )
        ctx = FlowContext(session, flow_values=values)
        resolved: list[tuple[int, int]] = []
        ctx._executor = SimpleNamespace(
            _execute_children=lambda _commands, child_ctx: resolved.append(
                (
                    int(
                        child_ctx.resolve_command_scalar(
                            BuiltinValueReference("_match_x").to_dict(),
                            ValueType.NUMBER,
                        )
                    ),
                    int(
                        child_ctx.resolve_command_scalar(
                            BuiltinValueReference("_match_y").to_dict(),
                            ValueType.NUMBER,
                        )
                    ),
                )
            )
        )

        with (
            patch("automadin.execution.handlers.windows.sys.platform", "win32"),
            patch(
                "automadin.execution.handlers.windows.WindowsDeviceEngine.resolve_session_data",
                return_value={"hwnd": 10, "title": "Game"},
            ),
            patch(
                "automadin.execution.handlers.windows._take_screenshot_for_device",
                return_value=b"png",
            ),
            patch(
                "automadin.execution.handlers.windows._find_any_image",
                return_value=("target", result),
            ),
        ):
            handle_win_if_image(command, ctx)

        self.assertEqual(resolved, [(45, 67)])
        with self.assertRaisesRegex(FlowRuntimeValueError, "unavailable in this scope"):
            values.resolve(BuiltinValueReference("_match_x"))

    def test_win_while_refreshes_match_coordinates_for_each_iteration(self) -> None:
        first = SimpleNamespace(center_x=11, center_y=22, confidence=0.95)
        second = SimpleNamespace(center_x=33, center_y=44, confidence=0.96)
        command = FlowCommand(
            "win_while",
            params={"images": ["target"], "timeout": 1000, "interval_ms": 0},
            children=[FlowCommand("win_click")],
        )
        flow = AutomationFlow(
            "Moving Windows image coordinates",
            [command],
            value_model_version=CURRENT_FLOW_VALUE_MODEL_VERSION,
        )
        values = FlowInvocationValues(flow.flow_id, flow.value_catalog, {})
        session = DeviceSession(
            device=WindowsDevice(hwnd=10, title="Game"),
            data={"windows_data": {"hwnd": 10, "title": "Game"}},
        )
        ctx = FlowContext(session, flow_values=values)
        resolved: list[tuple[int, int]] = []
        ctx._executor = SimpleNamespace(
            _execute_children=lambda _commands, child_ctx: resolved.append(
                (
                    int(
                        child_ctx.resolve_command_scalar(
                            BuiltinValueReference("_match_x").to_dict(),
                            ValueType.NUMBER,
                        )
                    ),
                    int(
                        child_ctx.resolve_command_scalar(
                            BuiltinValueReference("_match_y").to_dict(),
                            ValueType.NUMBER,
                        )
                    ),
                )
            )
        )

        with (
            patch("automadin.execution.handlers.windows.sys.platform", "win32"),
            patch(
                "automadin.execution.handlers.windows.WindowsDeviceEngine.resolve_session_data",
                return_value={"hwnd": 10, "title": "Game"},
            ),
            patch(
                "automadin.execution.handlers.windows._take_screenshot_for_device",
                return_value=b"png",
            ),
            patch(
                "automadin.execution.handlers.windows._find_any_image",
                side_effect=[("target", first), ("target", second), (None, None)],
            ),
        ):
            handle_win_while(command, ctx)

        self.assertEqual(resolved, [(11, 22), (33, 44)])
        with self.assertRaisesRegex(FlowRuntimeValueError, "unavailable in this scope"):
            values.resolve(BuiltinValueReference("_match_x"))

    def test_win_while_capture_failure_is_not_reported_as_zero_iterations(self) -> None:
        ctx, _executor = _windows_context()
        cmd = FlowCommand(
            "win_while",
            params={"images": ["target"], "timeout": 100},
        )

        with (
            patch("automadin.execution.handlers.windows.sys.platform", "win32"),
            patch(
                "automadin.execution.handlers.windows.WindowsDeviceEngine.resolve_session_data",
                return_value={"hwnd": 10, "title": "Game"},
            ),
            patch(
                "automadin.execution.handlers.windows._take_screenshot_for_device",
                return_value=None,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "failed to take screenshot"):
                handle_win_while(cmd, ctx)

    def test_positive_timeout_polls_and_clicks_final_match_once(self) -> None:
        ctx, executor = _windows_context()
        result = SimpleNamespace(center_x=20, center_y=30, confidence=0.95)
        cmd = FlowCommand(
            "win_if_image",
            params={
                "images": ["target"],
                "timeout": 100,
                "click_if_found": True,
            },
            children=[FlowCommand("then")],
            else_children=[FlowCommand("else")],
        )

        with (
            patch("automadin.execution.handlers.windows.sys.platform", "win32"),
            patch(
                "automadin.execution.handlers.windows.WindowsDeviceEngine.resolve_session_data",
                return_value={"hwnd": 10, "title": "Game"},
            ),
            patch(
                "automadin.execution.handlers.windows.time.monotonic",
                side_effect=[1.0, 1.0, 1.01],
            ),
            patch("automadin.execution.handlers.windows.time.sleep") as sleep,
            patch(
                "automadin.execution.handlers.windows._take_screenshot_for_device",
                side_effect=[b"first", b"second"],
            ),
            patch(
                "automadin.execution.handlers.windows._find_any_image",
                side_effect=[(None, None), ("target", result)],
            ),
            patch(
                "automadin.execution.handlers.windows._win_click_at_match"
            ) as click,
        ):
            handle_win_if_image(cmd, ctx)

        sleep.assert_called_once_with(0.05)
        click.assert_called_once()
        self.assertIs(click.call_args.args[1], result)
        self.assertEqual(ctx._last_screenshot, b"second")
        self.assertEqual(
            {
                name: ctx.get_variable(name)
                for name in (
                    "_image_found",
                    "_match_x",
                    "_match_y",
                    "_match_confidence",
                    "_matched_image",
                )
            },
            {
                "_image_found": "true",
                "_match_x": "20",
                "_match_y": "30",
                "_match_confidence": "0.9500",
                "_matched_image": "target",
            },
        )
        self.assertEqual(executor.calls, [["then"]])

    def test_not_found_condition_succeeds_without_click(self) -> None:
        ctx, executor = _windows_context()
        cmd = FlowCommand(
            "win_if_image",
            params={
                "images": ["target"],
                "condition": "not_found",
                "timeout": 100,
                "click_if_found": True,
            },
            children=[FlowCommand("then")],
            else_children=[FlowCommand("else")],
        )

        with (
            patch("automadin.execution.handlers.windows.sys.platform", "win32"),
            patch(
                "automadin.execution.handlers.windows.WindowsDeviceEngine.resolve_session_data",
                return_value={"hwnd": 10, "title": "Game"},
            ),
            patch(
                "automadin.execution.handlers.windows.time.monotonic",
                return_value=1.0,
            ),
            patch(
                "automadin.execution.handlers.windows._take_screenshot_for_device",
                return_value=b"png",
            ),
            patch(
                "automadin.execution.handlers.windows._find_any_image",
                return_value=(None, None),
            ),
            patch(
                "automadin.execution.handlers.windows._win_click_at_match"
            ) as click,
        ):
            handle_win_if_image(cmd, ctx)

        click.assert_not_called()
        self.assertEqual(executor.calls, [["then"]])

    def test_timeout_runs_else_once_and_bounds_sleep(self) -> None:
        ctx, executor = _windows_context()
        result = SimpleNamespace(center_x=20, center_y=30, confidence=0.95)
        cmd = FlowCommand(
            "win_if_image",
            params={
                "images": ["target"],
                "condition": "not_found",
                "timeout": 20,
            },
            children=[FlowCommand("then")],
            else_children=[FlowCommand("else")],
        )

        with (
            patch("automadin.execution.handlers.windows.sys.platform", "win32"),
            patch(
                "automadin.execution.handlers.windows.WindowsDeviceEngine.resolve_session_data",
                return_value={"hwnd": 10, "title": "Game"},
            ),
            patch(
                "automadin.execution.handlers.windows.time.monotonic",
                side_effect=[3.0, 3.005, 3.020],
            ),
            patch("automadin.execution.handlers.windows.time.sleep") as sleep,
            patch(
                "automadin.execution.handlers.windows._take_screenshot_for_device",
                return_value=b"first",
            ),
            patch(
                "automadin.execution.handlers.windows._find_any_image",
                return_value=("target", result),
            ) as find,
        ):
            handle_win_if_image(cmd, ctx)

        find.assert_called_once()
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.015)
        self.assertEqual(executor.calls, [["else"]])

    def test_stop_during_windows_wait_prevents_more_checks_and_branches(self) -> None:
        ctx, executor = _windows_context()
        cmd = FlowCommand(
            "win_if_image",
            params={"images": ["target"], "timeout": 100},
            children=[FlowCommand("then")],
            else_children=[FlowCommand("else")],
        )

        with (
            patch("automadin.execution.handlers.windows.sys.platform", "win32"),
            patch(
                "automadin.execution.handlers.windows.WindowsDeviceEngine.resolve_session_data",
                return_value={"hwnd": 10, "title": "Game"},
            ),
            patch(
                "automadin.execution.handlers.windows.time.monotonic",
                side_effect=[5.0, 5.0],
            ),
            patch(
                "automadin.execution.handlers.windows.time.sleep",
                side_effect=lambda _seconds: ctx.stop(),
            ),
            patch(
                "automadin.execution.handlers.windows._take_screenshot_for_device",
                return_value=b"png",
            ) as screenshot,
            patch(
                "automadin.execution.handlers.windows._find_any_image",
                return_value=(None, None),
            ) as find,
        ):
            handle_win_if_image(cmd, ctx)

        screenshot.assert_called_once()
        find.assert_called_once()
        self.assertEqual(executor.calls, [])


if __name__ == "__main__":
    unittest.main()
