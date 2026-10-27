from __future__ import annotations

import unittest

from logreduce.config import ConfigError, compile_rules, parse_config


class ConfigTests(unittest.TestCase):
    def test_parses_rules_and_compiles_them(self) -> None:
        config = parse_config(
            {
                "version": 1,
                "output": "combined.log",
                "rules": [
                    {
                        "name": "errors",
                        "filter": {"mode": "include", "pattern": "ERROR"},
                        "replace": {
                            "pattern": r"id=(\d+)",
                            "replacement": r"id=<\1>",
                        },
                    }
                ],
            }
        )

        self.assertEqual(config.output, "combined.log")
        self.assertEqual(config.encoding, "utf-8")
        compiled = compile_rules(config.rules)
        self.assertTrue(compiled[0].include_matches)
        self.assertEqual(compiled[0].name, "errors")

    def test_empty_rules_are_allowed(self) -> None:
        config = parse_config({"version": 1, "output": "out.log"})
        self.assertEqual(config.rules, ())

    def test_rejects_unknown_fields(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unknown field"):
            parse_config({"version": 1, "output": "out.log", "typo": True})

    def test_rejects_output_traversal(self) -> None:
        for output in ("../out.log", "/tmp/out.log", "dir/../../out.log"):
            with self.subTest(output=output):
                with self.assertRaises(ConfigError):
                    parse_config({"version": 1, "output": output})

    def test_rejects_rule_without_action(self) -> None:
        with self.assertRaisesRegex(ConfigError, "at least one"):
            parse_config(
                {
                    "version": 1,
                    "output": "out.log",
                    "rules": [{"name": "empty"}],
                }
            )

    def test_rejects_invalid_regex(self) -> None:
        with self.assertRaisesRegex(ConfigError, "invalid regex"):
            parse_config(
                {
                    "version": 1,
                    "output": "out.log",
                    "rules": [
                        {"filter": {"mode": "include", "pattern": "["}}
                    ],
                }
            )

    def test_rejects_unknown_encoding(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unknown text encoding"):
            parse_config(
                {"version": 1, "output": "out.log", "encoding": "not-a-codec"}
            )


if __name__ == "__main__":
    unittest.main()
