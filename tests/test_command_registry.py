from __future__ import annotations

import unittest

from automadin.execution.build_executor import build_executor
from automadin.execution.handlers.registry import (
    get_default_params,
    load_command_types,
    validate_command_types,
)


class CommandRegistryTests(unittest.TestCase):
    def test_all_command_definitions_have_registered_handlers(self) -> None:
        executor = build_executor()
        self.assertEqual(frozenset(load_command_types()), executor.registered_handler_ids)

    def test_duplicate_command_ids_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Duplicate command id"):
            validate_command_types(
                [
                    {"id": "same", "params": []},
                    {"id": "same", "params": []},
                ]
            )

    def test_unknown_parameter_types_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown type"):
            validate_command_types(
                [{"id": "example", "params": [{"name": "value", "type": "mystery"}]}]
            )

    def test_invalid_value_semantics_metadata_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown valueRole"):
            validate_command_types(
                [
                    {
                        "id": "example",
                        "params": [
                            {
                                "name": "value",
                                "type": "string",
                                "valueRole": "mystery",
                            }
                        ],
                    }
                ]
            )
        with self.assertRaisesRegex(ValueError, "childBuiltins"):
            validate_command_types(
                [
                    {
                        "id": "example",
                        "childBuiltins": ["index", "index"],
                        "params": [],
                    }
                ]
            )

    def test_value_reference_metadata_requires_a_typed_scalar_contract(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "supportsValueReference must be a boolean"
        ):
            validate_command_types(
                [
                    {
                        "id": "example",
                        "params": [
                            {
                                "name": "value",
                                "type": "int",
                                "supportsValueReference": "yes",
                                "allowed_value_types": ["integer"],
                            }
                        ],
                    }
                ]
            )

        with self.assertRaisesRegex(
            ValueError, "supportsValueReference requires allowed_value_types"
        ):
            validate_command_types(
                [
                    {
                        "id": "example",
                        "params": [
                            {
                                "name": "value",
                                "type": "int",
                                "supportsValueReference": True,
                            }
                        ],
                    }
                ]
            )

        with self.assertRaisesRegex(
            ValueError, "requires an int, double, or bool parameter"
        ):
            validate_command_types(
                [
                    {
                        "id": "example",
                        "params": [
                            {
                                "name": "value",
                                "type": "string",
                                "supportsValueReference": True,
                                "allowed_value_types": ["text"],
                            }
                        ],
                    }
                ]
            )

        command_types = load_command_types()
        self.assertEqual(
            next(
                parameter
                for parameter in command_types["wait"]["params"]
                if parameter["name"] == "pause_ms"
            )["allowed_value_types"],
            ["duration_ms"],
        )

    def test_literal_brace_metadata_requires_a_text_parameter(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "allowsLiteralBraces must be a boolean"
        ):
            validate_command_types(
                [
                    {
                        "id": "example",
                        "params": [
                            {
                                "name": "script",
                                "type": "multiline",
                                "allowsLiteralBraces": "yes",
                            }
                        ],
                    }
                ]
            )

        with self.assertRaisesRegex(
            ValueError, "requires a string or multiline parameter"
        ):
            validate_command_types(
                [
                    {
                        "id": "example",
                        "params": [
                            {
                                "name": "count",
                                "type": "int",
                                "allowsLiteralBraces": True,
                            }
                        ],
                    }
                ]
            )

        script_param = next(
            parameter
            for parameter in load_command_types()["evaluate_js"]["params"]
            if parameter["name"] == "script"
        )
        self.assertIs(script_param["allowsLiteralBraces"], True)

    def test_duplicate_and_invalid_parameter_aliases_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate parameter or alias"):
            validate_command_types(
                [
                    {
                        "id": "example",
                        "params": [
                            {
                                "name": "value",
                                "type": "string",
                                "aliases": ["legacy"],
                            },
                            {"name": "legacy", "type": "string"},
                        ],
                    }
                ]
            )
        with self.assertRaisesRegex(ValueError, "aliases must be text"):
            validate_command_types(
                [
                    {
                        "id": "example",
                        "params": [
                            {"name": "value", "type": "string", "aliases": [1]}
                        ],
                    }
                ]
            )

    def test_invalid_metadata_defaults_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid default"):
            validate_command_types(
                [
                    {
                        "id": "example",
                        "params": [
                            {"name": "count", "type": "int", "default": "three"}
                        ],
                    }
                ]
            )
        with self.assertRaisesRegex(ValueError, "default is not an option"):
            validate_command_types(
                [
                    {
                        "id": "example",
                        "params": [
                            {
                                "name": "mode",
                                "type": "enum",
                                "options": ["one", "two"],
                                "default": "three",
                            }
                        ],
                    }
                ]
            )

    def test_explicit_defaults_override_type_heuristics(self) -> None:
        command_type = {
            "id": "example",
            "params": [
                {"name": "count", "type": "int", "default": 7},
                {
                    "name": "mode",
                    "type": "enum",
                    "options": ["first", "second"],
                    "default": "second",
                },
            ],
        }

        self.assertEqual(get_default_params(command_type), {"count": 7, "mode": "second"})

    def test_locator_parameter_accepts_structured_and_legacy_defaults(self) -> None:
        for default in (
            "#submit",
            {"strategy": "role", "role": "button", "name": "Submit"},
            "",
        ):
            entries = validate_command_types(
                [
                    {
                        "id": "example",
                        "params": [
                            {"name": "target", "type": "locator", "default": default}
                        ],
                    }
                ]
            )
            self.assertEqual(entries[0]["params"][0]["default"], default)

        with self.assertRaisesRegex(ValueError, "invalid default"):
            validate_command_types(
                [
                    {
                        "id": "example",
                        "params": [
                            {"name": "target", "type": "locator", "default": {}}
                        ],
                    }
                ]
            )

    def test_required_locator_uses_empty_legacy_default(self) -> None:
        command_type = {
            "id": "example",
            "params": [{"name": "target", "type": "locator"}],
        }

        self.assertEqual(get_default_params(command_type), {"target": ""})

    def test_optional_params_without_explicit_defaults_are_omitted(self) -> None:
        command_type = {
            "id": "example",
            "params": [
                {"name": "label", "type": "string"},
                {"name": "values", "type": "string_list"},
                {"name": "pause_ms", "type": "int", "optional": True},
                {"name": "enabled", "type": "bool", "optional": True, "default": True},
            ],
        }

        self.assertEqual(
            get_default_params(command_type),
            {"label": "", "values": [], "enabled": True},
        )

    def test_control_flow_defaults_are_declared_authoritatively(self) -> None:
        command_types = load_command_types()

        self.assertEqual(get_default_params(command_types["for"])["count"], 3)
        self.assertEqual(
            get_default_params(command_types["if_elapsed"])["min_wait_time"],
            0,
        )

    def test_hook_points_are_typed_and_evidence_checked(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires screenshot evidence"):
            validate_command_types(
                [
                    {
                        "id": "bad",
                        "hookPoints": [
                            {
                                "type": "visual_image",
                                "evidence": "browser_dom",
                                "phases": ["before"],
                            }
                        ],
                        "params": [],
                    }
                ]
            )

        with self.assertRaisesRegex(ValueError, "unique before/after"):
            validate_command_types(
                [
                    {
                        "id": "bad",
                        "hookPoints": [
                            {
                                "type": "browser_element",
                                "evidence": "browser_dom",
                                "phases": ["before", "before"],
                            }
                        ],
                        "params": [],
                    }
                ]
            )

        with self.assertRaisesRegex(ValueError, "capture must be"):
            validate_command_types(
                [
                    {
                        "id": "bad",
                        "hookPoints": [
                            {
                                "type": "visual_image",
                                "evidence": "screenshot",
                                "capture": "sometimes",
                                "phases": ["before"],
                            }
                        ],
                        "params": [],
                    }
                ]
            )


if __name__ == "__main__":
    unittest.main()
