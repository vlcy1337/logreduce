from __future__ import annotations

import unittest

from logreduce.config import parse_config
from logreduce.errors import ConfigError
from logreduce.rules import (
    Ruleset,
    _can_match_newline,
    _replacement_can_insert_newline,
)


def ruleset(*rules):
    return Ruleset(parse_config({"version": 1, "rules": list(rules)}).rules)


def rule(action, pattern, **kwargs):
    data = {
        "action": action,
        "match_type": kwargs.pop("match_type", "regex"),
        "pattern": pattern,
    }
    data.update(kwargs)
    return data


class RuleTests(unittest.TestCase):
    def test_drop_literal_and_regex(self):
        rs = ruleset(rule("drop", "DEBUG", match_type="literal"))
        self.assertIsNone(rs.process_line(b"a DEBUG b"))
        self.assertEqual(rs.process_line(b"a INFO b"), b"a INFO b")

        rs = ruleset(rule("drop", r"^DEBUG\b"))
        self.assertIsNone(rs.process_line(b"DEBUG x"))
        self.assertEqual(rs.process_line(b"x DEBUG"), b"x DEBUG")

    def test_keep(self):
        rs = ruleset(rule("keep", r"ERROR|WARN"))
        self.assertEqual(rs.process_line(b"ERROR x"), b"ERROR x")
        self.assertIsNone(rs.process_line(b"INFO x"))

    def test_multiple_keeps_are_intersection(self):
        rs = ruleset(rule("keep", "a"), rule("keep", "b"))
        self.assertEqual(rs.process_line(b"a b"), b"a b")
        self.assertIsNone(rs.process_line(b"a"))
        self.assertIsNone(rs.process_line(b"b"))

    def test_replace_groups_and_order(self):
        rs = ruleset(
            rule("replace", r"(token=)\w+", replacement=r"\1***"),
            rule("replace", "secret", match_type="literal", replacement="hidden"),
        )
        self.assertEqual(rs.process_line(b"token=abc secret"), b"token=*** hidden")

    def test_later_rule_sees_earlier_replacement(self):
        rs = ruleset(
            rule("replace", "old", match_type="literal", replacement="new"),
            rule("drop", "new", match_type="literal"),
        )
        self.assertIsNone(rs.process_line(b"old value"))

    def test_when_literal(self):
        rs = ruleset(
            rule(
                "replace",
                "token=\\w+",
                replacement="token=***",
                when="ERROR",
            )
        )
        self.assertEqual(rs.process_line(b"ERROR token=abc"), b"ERROR token=***")
        self.assertEqual(rs.process_line(b"INFO token=abc"), b"INFO token=abc")

    def test_when_regex(self):
        rs = ruleset(
            rule(
                "replace",
                "token=\\w+",
                replacement="token=***",
                when=r"^ERROR\b",
                when_type="regex",
                when_flags="i",
            )
        )
        self.assertEqual(rs.process_line(b"error token=abc"), b"error token=***")
        self.assertEqual(rs.process_line(b"x error token=abc"), b"x error token=abc")

    def test_regex_flags(self):
        rs = ruleset(rule("drop", "debug", flags="i"))
        self.assertIsNone(rs.process_line(b"DEBUG"))
        self.assertIsNone(rs.process_line(b"DeBuG"))

    def test_bulk_literal(self):
        rs = ruleset(
            rule("replace", "old", match_type="literal", replacement="new"),
            rule("replace", "x", match_type="literal", replacement="y"),
        )
        self.assertIsNotNone(rs.bulk_ops)
        self.assertFalse(rs.bulk_ops.has_regex)
        self.assertEqual(rs.process_line(b"old x"), b"new y")

    def test_invalid_replacement_group(self):
        with self.assertRaises(ConfigError):
            ruleset(rule("replace", r"(a)", replacement=r"\2"))

    def test_bulk_regex_safe(self):
        rs = ruleset(
            rule("replace", r"(token=)[A-Za-z0-9._-]+", replacement=r"\1***")
        )
        self.assertIsNotNone(rs.bulk_ops)
        self.assertTrue(rs.bulk_ops.has_regex)

    def test_bulk_disabled_by_when(self):
        rs = ruleset(
            rule(
                "replace",
                "token=\\w+",
                replacement="token=***",
                when="ERROR",
            )
        )
        self.assertIsNone(rs.bulk_ops)

    def test_bulk_disabled_by_newline_pattern(self):
        self.assertIsNone(ruleset(rule("replace", r"\s+", replacement=" ")).bulk_ops)
        self.assertIsNone(ruleset(rule("replace", r"[^a]", replacement="x")).bulk_ops)
        self.assertIsNone(ruleset(rule("replace", r".*", replacement="x")).bulk_ops)
        self.assertIsNone(ruleset(rule("replace", r"^", replacement="x")).bulk_ops)

    def test_bulk_disabled_by_replacement_newline(self):
        self.assertIsNone(
            ruleset(rule("replace", "x", replacement="a\nb")).bulk_ops
        )
        self.assertIsNone(
            ruleset(rule("replace", "x", replacement=r"\n")).bulk_ops
        )
        self.assertIsNone(
            ruleset(rule("replace", "x", replacement=r"\012")).bulk_ops
        )
        with self.assertRaises(ConfigError):
            ruleset(rule("replace", "x", replacement=r"\x0a"))
        self.assertIsNotNone(
            ruleset(rule("replace", r"(x)", replacement=r"\1")).bulk_ops
        )
        self.assertIsNotNone(
            ruleset(rule("replace", "x", replacement=r"\\n")).bulk_ops
        )

    def test_replacement_can_insert_newline(self):
        self.assertTrue(_replacement_can_insert_newline(b"\\n"))
        self.assertTrue(_replacement_can_insert_newline(b"\\r"))
        self.assertTrue(_replacement_can_insert_newline(b"\\x0a"))
        self.assertTrue(_replacement_can_insert_newline(b"\\x0D"))
        self.assertTrue(_replacement_can_insert_newline(b"\\012"))
        self.assertTrue(_replacement_can_insert_newline(b"\\u000a"))
        self.assertFalse(_replacement_can_insert_newline(b"\\1"))
        self.assertFalse(_replacement_can_insert_newline(b"\\g<0>"))
        self.assertFalse(_replacement_can_insert_newline(b"\\t"))

    def test_can_match_newline(self):
        for pattern in (
            r"\n",
            r"\r",
            r"\s",
            r"\W",
            r"\D",
            r"[\s]",
            r"[^a]",
            r"[\t-\x0c]",
            r"[\b-\x0b]",
            r"foo\nbar",
            r"\x0a",
            r"\012",
        ):
            self.assertTrue(_can_match_newline(pattern), pattern)

        for pattern in (
            r"foo",
            r"(token=)[A-Za-z0-9._-]+",
            r"\S+",
            r"\w+",
            r"\d+",
            r"[\x41-\x5a]",
            r"^foo$",
            r"foo.*bar",
        ):
            self.assertFalse(_can_match_newline(pattern), pattern)


if __name__ == "__main__":
    unittest.main()
