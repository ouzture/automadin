import unittest

from automadin.execution.context import FlowContext
from automadin.execution.handlers.control import _parse_for_each_items, handle_for_each
from automadin.models.device import DeviceSession
from automadin.models.flow import FlowCommand


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def _execute_children(self, commands: list[FlowCommand], ctx: FlowContext) -> None:
        self.calls.append((ctx.interpolate("{{item}}"), ctx.interpolate("{{index}}")))


class ForEachValueTests(unittest.TestCase):
    def test_parse_legacy_newline_string_values(self) -> None:
        self.assertEqual(
            _parse_for_each_items("alpha\n\n beta \ngamma "),
            ["alpha", "beta", "gamma"],
        )

    def test_parse_legacy_comma_string_values(self) -> None:
        self.assertEqual(
            _parse_for_each_items("alpha, beta,, gamma "),
            ["alpha", "beta", "gamma"],
        )

    def test_parse_list_values_preserves_order_and_ignores_blanks(self) -> None:
        self.assertEqual(
            _parse_for_each_items([" alpha ", "", 42, None, "gamma"]),
            ["alpha", "42", "gamma"],
        )

    def test_handle_for_each_sets_item_and_index_for_list_values(self) -> None:
        ctx = FlowContext(DeviceSession())
        executor = _RecordingExecutor()
        ctx._executor = executor
        cmd = FlowCommand(
            id="for_each",
            params={"values": ["alpha", "beta"]},
            children=[FlowCommand(id="input_fill")],
        )

        handle_for_each(cmd, ctx)

        self.assertEqual(executor.calls, [("alpha", "0"), ("beta", "1")])

    def test_handle_for_each_interpolates_list_values(self) -> None:
        ctx = FlowContext(DeviceSession())
        ctx.set_variable("name", "resolved")
        executor = _RecordingExecutor()
        ctx._executor = executor
        cmd = FlowCommand(
            id="for_each",
            params={"values": ["{{name}}"]},
            children=[FlowCommand(id="input_fill")],
        )

        handle_for_each(cmd, ctx)

        self.assertEqual(executor.calls, [("resolved", "0")])

    def test_string_list_default_params_are_empty_lists(self) -> None:
        from automadin.execution.handlers.registry import get_default_params

        command_type = {
            "id": "example",
            "params": [{"name": "values", "type": "string_list"}],
        }

        self.assertEqual(get_default_params(command_type), {"values": []})


if __name__ == "__main__":
    unittest.main()
