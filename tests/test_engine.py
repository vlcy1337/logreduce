from __future__ import annotations

import io
import os
import stat
import tempfile
import unittest
from pathlib import Path

from logreduce.config import parse_config
from logreduce.engine import (
    process_batch,
    process_file,
    process_inplace,
)
from logreduce.models import SourceFile
from logreduce.rules import Ruleset


def make_ruleset(*rules):
    return Ruleset(parse_config({"version": 1, "rules": list(rules)}).rules)


def rule(action, pattern, **kwargs):
    data = {
        "action": action,
        "match_type": kwargs.pop("match_type", "regex"),
        "pattern": pattern,
    }
    data.update(kwargs)
    return data


def source_for(path: Path) -> SourceFile:
    st = path.stat()
    return SourceFile(
        path=path,
        rel=path.name,
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        dev=st.st_dev,
        ino=st.st_ino,
    )


class EngineTests(unittest.TestCase):
    @staticmethod
    def _reference(data: bytes, ruleset: Ruleset) -> bytes:
        parts = data.split(b"\n")
        carry = parts.pop()
        out: list[bytes] = []
        for raw in parts:
            if raw.endswith(b"\r"):
                body = raw[:-1]
                ending = b"\r\n"
            else:
                body = raw
                ending = b"\n"
            result = ruleset.process_line(body)
            if result is None:
                continue
            if result is body:
                out.append(raw)
                out.append(b"\n")
            else:
                out.append(result)
                out.append(ending)
        if carry:
            result = ruleset.process_line(carry)
            if result is not None:
                out.append(carry if result is carry else result)
        return b"".join(out)

    def test_bulk_vs_strict_line_fuzz(self):
        import random

        patterns = [
            (r"(token=)[A-Za-z0-9._-]+", r"\1***"),
            (r"foo$", "FOO"),
            (r"^foo", "BAR"),
            (r"\bfoo\b", "X"),
            (r"[0-9]+", "N"),
            (r"(?i)error", "ERR"),
            (r"\S+@\S+", "EMAIL"),
        ]
        samples = [
            b"foo",
            b"foo bar",
            b"token=abc",
            b"error",
            b"123 456",
            b"a@b.com",
            b"\xff",
            b"Foofoo",
            b"",
        ]
        random.seed(7)
        for pattern, replacement in patterns:
            ruleset = make_ruleset(
                rule("replace", pattern, replacement=replacement)
            )
            self.assertIsNotNone(ruleset.bulk_ops, pattern)
            for _ in range(40):
                lines = []
                for _ in range(random.randint(0, 20)):
                    text = random.choice(samples)
                    if random.random() < 0.4:
                        text += b" extra"
                    ending = random.choice((b"\n", b"\r\n"))
                    lines.append(text + ending)
                data = b"".join(lines)
                if data.endswith(b"\n") and random.random() < 0.5:
                    data = data[:-1]
                output, _ = self._process(data, ruleset, chunk_size=7)
                self.assertEqual(
                    output,
                    self._reference(data, ruleset),
                    (pattern, data),
                )

    def _process(self, data: bytes, ruleset: Ruleset, chunk_size: int = 8):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.log"
            path.write_bytes(data)
            out = io.BytesIO()
            stats = process_file(path, out, ruleset, chunk_size)
            return out.getvalue(), stats

    def test_drop_keep_replace(self):
        rs = make_ruleset(
            rule("drop", r"^DEBUG\b"),
            rule("keep", r"ERROR|INFO"),
            rule("replace", "old", match_type="literal", replacement="new"),
        )
        output, stats = self._process(
            b"DEBUG x\nINFO old\nWARN old\nERROR old\n", rs, chunk_size=4
        )
        self.assertEqual(output, b"INFO new\nERROR new\n")
        self.assertEqual(stats.lines_in, 4)
        self.assertEqual(stats.lines_out, 2)
        self.assertEqual(stats.lines_dropped, 2)

    def test_crlf_preserved(self):
        rs = make_ruleset(rule("replace", "old", match_type="literal", replacement="new"))
        output, _ = self._process(b"a old\r\nb old\r\n", rs)
        self.assertEqual(output, b"a new\r\nb new\r\n")

    def test_no_trailing_newline(self):
        rs = make_ruleset(rule("replace", "old", match_type="literal", replacement="new"))
        output, stats = self._process(b"a old\nb old", rs)
        self.assertEqual(output, b"a new\nb new")
        self.assertEqual(stats.lines_in, 2)
        self.assertEqual(stats.last_byte, ord("w"))

    def test_empty_file(self):
        rs = make_ruleset(rule("drop", "x"))
        output, stats = self._process(b"", rs)
        self.assertEqual(output, b"")
        self.assertEqual(stats.lines_in, 0)
        self.assertIsNone(stats.last_byte)

    def test_only_newline(self):
        rs = make_ruleset(rule("keep", ".*"))
        output, stats = self._process(b"\n", rs)
        self.assertEqual(output, b"\n")
        self.assertEqual(stats.lines_in, 1)
        self.assertEqual(stats.lines_out, 1)

    def test_invalid_utf8_preserved(self):
        rs = make_ruleset(rule("replace", "x", match_type="literal", replacement="y"))
        output, _ = self._process(b"\xff\xfe x\n", rs)
        self.assertEqual(output, b"\xff\xfe y\n")

    def test_line_longer_than_chunk(self):
        rs = make_ruleset(rule("drop", "DROP"))
        data = b"a" * 20 + b"\nDROP\n" + b"b" * 20
        output, stats = self._process(data, rs, chunk_size=4)
        self.assertEqual(output, b"a" * 20 + b"\n" + b"b" * 20)
        self.assertEqual(stats.lines_in, 3)

    def test_bulk_regex_crlf_fallback(self):
        rs = make_ruleset(
            rule("replace", r"foo$", replacement="FOO"),
            rule("replace", r"(token=)\w+", replacement=r"\1***"),
        )
        self.assertIsNotNone(rs.bulk_ops)
        output, _ = self._process(
            b"token=abc foo\r\nfoo\r\ntoken=xyz foo\n", rs, chunk_size=8
        )
        self.assertEqual(
            output, b"token=*** FOO\r\nFOO\r\ntoken=*** FOO\n"
        )

    def test_bulk_literal_equivalence(self):
        rs = make_ruleset(
            rule("replace", "a", match_type="literal", replacement="aa"),
            rule("replace", "aa", match_type="literal", replacement="b"),
        )
        data = b"a a\nbanana\n" + b"x" * 10
        output, _ = self._process(data, rs, chunk_size=3)
        expected = b"\n".join(
            rs.process_line(line) or b""
            for line in data.split(b"\n")
        )
        self.assertEqual(output, expected)

    def test_bulk_stats(self):
        rs = make_ruleset(rule("replace", "x", match_type="literal", replacement="y"))
        output, stats = self._process(b"x\nx\nx", rs, chunk_size=4)
        self.assertEqual(output, b"y\ny\ny")
        self.assertEqual(stats.lines_in, 3)
        self.assertEqual(stats.lines_out, 3)
        self.assertEqual(stats.lines_dropped, 0)

    def test_change_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.log"
            path.write_bytes(b"abc\n")
            out = io.BytesIO()
            with self.assertRaises(Exception):
                process_file(
                    path,
                    out,
                    make_ruleset(rule("drop", "x")),
                    8,
                    expected=(999, 999),
                )

    def test_inplace_preserves_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.log"
            path.write_bytes(b"old\n")
            os.chmod(path, 0o640)
            source = source_for(path)
            stats = process_inplace(
                source,
                make_ruleset(
                    rule("replace", "old", match_type="literal", replacement="new")
                ),
                8,
                False,
                False,
            )
            self.assertEqual(path.read_bytes(), b"new\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
            self.assertEqual(stats.bytes_out, 4)

    def test_batch_separator_and_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a.log"
            empty = root / "empty.log"
            b = root / "b.log"
            a.write_bytes(b"a")
            empty.write_bytes(b"")
            b.write_bytes(b"b\n")
            sources = [source_for(a), source_for(empty), source_for(b)]
            rs = make_ruleset(rule("replace", "zzz", match_type="literal", replacement="y"))
            shard = root / "shard.part"
            stats, last_byte = process_batch(
                sources, shard, b"\n", rs, 8, False, False
            )
            self.assertEqual(shard.read_bytes(), b"a\nb\n")
            self.assertEqual(last_byte, 10)
            self.assertEqual([s.bytes_out for s in stats], [1, 0, 2])

    def test_batch_all_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a.log"
            b = root / "b.log"
            a.write_bytes(b"")
            b.write_bytes(b"")
            sources = [source_for(a), source_for(b)]
            rs = make_ruleset(rule("replace", "x", replacement="y"))
            shard = root / "shard.part"
            _, last_byte = process_batch(
                sources, shard, b"\n", rs, 8, False, False
            )
            self.assertEqual(shard.read_bytes(), b"")
            self.assertIsNone(last_byte)


if __name__ == "__main__":
    unittest.main()
