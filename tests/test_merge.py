from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from logreduce.merge import delete_sources, merge_paths
from logreduce.models import SourceFile


class MergeTests(unittest.TestCase):
    def test_merge_separator_and_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a"
            b = root / "b"
            c = root / "c"
            a.write_bytes(b"a")
            b.write_bytes(b"b\n")
            c.write_bytes(b"")
            output = root / "out"
            merge_paths(
                [a, b, c],
                [ord("a"), 10, None],
                output,
                b"\n",
                False,
            )
            self.assertEqual(output.read_bytes(), b"a\nb\n")

    def test_merge_all_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a"
            a.write_bytes(b"")
            output = root / "out"
            merge_paths([a], [None], output, b"\n", False)
            self.assertEqual(output.read_bytes(), b"")

    def test_merge_empty_separator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a"
            b = root / "b"
            a.write_bytes(b"a")
            b.write_bytes(b"b")
            output = root / "out"
            merge_paths([a, b], [ord("a"), ord("b")], output, b"", False)
            self.assertEqual(output.read_bytes(), b"ab")

    def test_merge_group_ids_do_not_add_separator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a"
            b = root / "b"
            a.write_bytes(b"a")
            b.write_bytes(b"b")
            output = root / "out"
            merge_paths(
                [a, b],
                [ord("a"), ord("b")],
                output,
                b"\n",
                False,
                group_ids=[0, 0],
            )
            self.assertEqual(output.read_bytes(), b"ab")

    def test_merge_group_ids_add_separator_between_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a"
            b = root / "b"
            a.write_bytes(b"a")
            b.write_bytes(b"b")
            output = root / "out"
            merge_paths(
                [a, b],
                [ord("a"), ord("b")],
                output,
                b"\n",
                False,
                group_ids=[0, 1],
            )
            self.assertEqual(output.read_bytes(), b"a\nb")

    def test_delete_sources_and_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = root / "a"
            alias = root / "alias"
            a.write_bytes(b"x")
            os.link(a, alias)
            source = SourceFile(
                path=a,
                rel="a",
                size=1,
                mtime_ns=a.stat().st_mtime_ns,
                aliases=[alias],
            )
            errors = delete_sources([source], False)
            self.assertEqual(errors, [])
            self.assertFalse(a.exists())
            self.assertFalse(alias.exists())

    def test_delete_missing_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing"
            source = SourceFile(
                path=missing,
                rel="missing",
                size=0,
                mtime_ns=0,
            )
            self.assertEqual(delete_sources([source], False), [])


if __name__ == "__main__":
    unittest.main()
