from __future__ import annotations

import unittest

from automadin.models.flow import AutomationFlow, FlowCommand


class FlowValidationTests(unittest.TestCase):
    def test_flow_requires_a_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty string name"):
            AutomationFlow.from_dict({"commands": []})

    def test_command_params_must_be_an_object(self) -> None:
        with self.assertRaisesRegex(ValueError, "params must be an object"):
            FlowCommand.from_dict({"id": "wait", "params": []})

    def test_nested_command_validation_reports_the_command(self) -> None:
        with self.assertRaisesRegex(ValueError, "children must be arrays"):
            AutomationFlow.from_dict(
                {"name": "bad", "commands": [{"id": "for", "children": {}}]}
            )


if __name__ == "__main__":
    unittest.main()
