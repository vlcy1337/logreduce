from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from logreduce.config import DEFAULT_CHUNK_SIZE, load_config, parse_config
from logreduce.errors import ConfigError


def minimal(**rule_overrides):
    rule = {
        "action": "drop",
        "match_type": "literal",
        "pattern": "noise",
    }
    rule.update(rule_overrides)
    return {
        "version": 1,
        "rules": [rule],
    }


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = parse_config(minimal())
        self.assertEqual(cfg.version, 1)
        self.assertEqual(cfg.input.include, ("**/*.log",))
        self.assertEqual(cfg.input.exclude, ())
        self.assertFalse(cfg.input.follow_symlinks)
        self.assertIsNone(cfg.output.path)
        self.assertEqual(cfg.output.separator, "\n")
        self.assertTrue(cfg.output.delete_sources)
        self.assertEqual(cfg.engine.mode, "inplace")
        self.assertEqual(cfg.engine.jobs, 0)
        self.assertEqual(cfg.engine.chunk_size, DEFAULT_CHUNK_SIZE)
        self.assertTrue(cfg.engine.fsync)
        self.assertEqual(cfg.engine.sort, "path")
        self.assertEqual(len(cfg.rules), 1)

    def test_unknown_keys(self):
        with self.assertRaises(ConfigError):
            parse_config({**minimal(), "wat": 1})
        for section in ("input", "output", "engine"):
            data = minimal()
            data[section] = {"wat": 1}
            with self.assertRaises(ConfigError):
                parse_config(data)
        with self.assertRaises(ConfigError):
            parse_config(minimal(wat=1))

    def test_version(self):
        with self.assertRaises(ConfigError):
            parse_config({**minimal(), "version": 2})

    def test_rules_required(self):
        with self.assertRaises(ConfigError):
            parse_config({"version": 1})
        with self.assertRaises(ConfigError):
            parse_config({"version": 1, "rules": []})

    def test_replace_requires_replacement(self):
        with self.assertRaises(ConfigError):
            parse_config(minimal(action="replace"))
        cfg = parse_config(minimal(action="replace", replacement=""))
        self.assertEqual(cfg.rules[0].replacement, "")

    def test_when_only_for_replace(self):
        with self.assertRaises(ConfigError):
            parse_config(minimal(when="x"))

    def test_literal_newline_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config(minimal(pattern="a\nb"))
        with self.assertRaises(ConfigError):
            parse_config(minimal(pattern="a\rb"))

    def test_flags(self):
        with self.assertRaises(ConfigError):
            parse_config(minimal(action="replace", replacement="x", flags="q"))
        with self.assertRaises(ConfigError):
            parse_config(minimal(action="replace", replacement="x", flags="ii"))
        with self.assertRaises(ConfigError):
            parse_config(
                minimal(
                    action="replace",
                    match_type="literal",
                    replacement="x",
                    flags="i",
                )
            )
        cfg = parse_config(
            minimal(
                action="replace",
                match_type="regex",
                replacement="x",
                flags="im",
            )
        )
        self.assertEqual(cfg.rules[0].flags, "im")

    def test_when_types(self):
        cfg = parse_config(
            minimal(
                action="replace",
                replacement="x",
                when="ERROR",
                when_type="regex",
                when_flags="i",
            )
        )
        self.assertEqual(cfg.rules[0].when, "ERROR")
        self.assertEqual(cfg.rules[0].when_flags, "i")
        with self.assertRaises(ConfigError):
            parse_config(
                minimal(
                    action="replace",
                    replacement="x",
                    when="ERROR",
                    when_type="regex",
                    when_flags="q",
                )
            )
        with self.assertRaises(ConfigError):
            parse_config(
                minimal(
                    action="replace",
                    replacement="x",
                    when="ERROR",
                    when_type="literal",
                    when_flags="i",
                )
            )
        with self.assertRaises(ConfigError):
            parse_config(
                minimal(
                    action="replace",
                    replacement="x",
                    when="a\nb",
                )
            )

    def test_chunk_size(self):
        for value, expected in (
            (4096, 4096),
            ("4KiB", 4096),
            ("8MiB", 8 * 1024 * 1024),
            ("64MB", 64_000_000),
            ("1GiB", 1024**3),
        ):
            data = minimal()
            data["engine"] = {"chunk_size": value}
            self.assertEqual(parse_config(data).engine.chunk_size, expected)
        with self.assertRaises(ConfigError):
            parse_config({**minimal(), "engine": {"chunk_size": "1B"}})
        with self.assertRaises(ConfigError):
            parse_config({**minimal(), "engine": {"chunk_size": "8XB"}})

    def test_engine_and_input_values(self):
        data = minimal()
        data["input"] = {
            "include": ["**/*.log", "**/*.txt"],
            "exclude": ["**/archive/**"],
            "follow_symlinks": True,
        }
        data["output"] = {
            "path": "out.log",
            "separator": "",
            "delete_sources": False,
        }
        data["engine"] = {
            "mode": "direct",
            "jobs": 4,
            "fsync": False,
            "sort": "mtime",
            "allow_changing": True,
        }
        cfg = parse_config(data)
        self.assertEqual(cfg.input.include, ("**/*.log", "**/*.txt"))
        self.assertEqual(cfg.input.exclude, ("**/archive/**",))
        self.assertTrue(cfg.input.follow_symlinks)
        self.assertEqual(cfg.output.path, "out.log")
        self.assertEqual(cfg.output.separator, "")
        self.assertFalse(cfg.output.delete_sources)
        self.assertEqual(cfg.engine.mode, "direct")
        self.assertEqual(cfg.engine.jobs, 4)
        self.assertFalse(cfg.engine.fsync)
        self.assertEqual(cfg.engine.sort, "mtime")
        self.assertTrue(cfg.engine.allow_changing)

    def test_load_config_toml_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.toml"
            path.write_text("this is not = = toml", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_load_config_missing(self):
        with self.assertRaises(ConfigError):
            load_config("/nonexistent/logreduce.toml")


if __name__ == "__main__":
    unittest.main()
