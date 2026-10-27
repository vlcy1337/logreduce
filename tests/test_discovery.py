from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from logreduce.config import InputConfig
from logreduce.discovery import discover, glob_to_regex


class GlobTests(unittest.TestCase):
    def test_glob_translation(self):
        self.assertTrue(glob_to_regex("**/*.log").match("a.log"))
        self.assertTrue(glob_to_regex("**/*.log").match("dir/a.log"))
        self.assertFalse(glob_to_regex("**/*.log").match("dir/a.txt"))
        self.assertTrue(glob_to_regex("*.log").match("a.log"))
        self.assertFalse(glob_to_regex("*.log").match("dir/a.log"))
        self.assertTrue(glob_to_regex("dir/**").match("dir/a"))
        self.assertTrue(glob_to_regex("**/archive/**").match("archive/a"))
        self.assertTrue(glob_to_regex("**/archive/**").match("x/archive/a"))
        self.assertTrue(glob_to_regex("a?c").match("abc"))
        self.assertFalse(glob_to_regex("a?c").match("a/c"))
        self.assertTrue(glob_to_regex("a[0-9].log").match("a5.log"))
        self.assertTrue(glob_to_regex("a[!0-9].log").match("ax.log"))
        self.assertFalse(glob_to_regex("a[!0-9].log").match("a5.log"))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "logs"
        (self.root / "sub").mkdir(parents=True)
        (self.root / "a.log").write_bytes(b"a\n")
        (self.root / "sub" / "b.log").write_bytes(b"b\n")
        (self.root / "c.txt").write_bytes(b"c\n")
        (self.root / "d.gz").write_bytes(b"d\n")
        (self.root / ".hidden.log").write_bytes(b"h\n")

    def tearDown(self):
        self._tmp.cleanup()

    def config(self, **kwargs):
        return InputConfig(**{"include": ("**/*.log",), **kwargs})

    def test_include_and_sort(self):
        files = discover(self.root, self.config(), None, "path")
        self.assertEqual(
            [f.rel for f in files],
            [".hidden.log", "a.log", "sub/b.log"],
        )

    def test_exclude(self):
        files = discover(
            self.root,
            self.config(exclude=("sub/**", "**/*.gz")),
            None,
            "path",
        )
        self.assertEqual([f.rel for f in files], [".hidden.log", "a.log"])

    def test_symlink_file_skipped(self):
        link = self.root / "link.log"
        link.symlink_to(self.root / "a.log")
        files = discover(self.root, self.config(), None, "path")
        self.assertNotIn("link.log", [f.rel for f in files])

    def test_hardlink_deduped_and_alias(self):
        link = self.root / "link.log"
        os.link(self.root / "a.log", link)
        files = discover(self.root, self.config(), None, "path")
        rels = [f.rel for f in files]
        self.assertIn("a.log", rels)
        self.assertNotIn("link.log", rels)
        primary = next(f for f in files if f.rel == "a.log")
        self.assertEqual([p.name for p in primary.aliases], ["link.log"])

    def test_output_excluded(self):
        output = self.root / "merged.log"
        output.write_bytes(b"old\n")
        files = discover(self.root, self.config(), output, "path")
        self.assertNotIn("merged.log", [f.rel for f in files])

    def test_output_hardlink_alias_skipped(self):
        output = self.root / "merged.log"
        os.link(self.root / "a.log", output)
        files = discover(self.root, self.config(), output, "path")
        rels = [f.rel for f in files]
        self.assertNotIn("merged.log", rels)
        self.assertIn("a.log", rels)
        primary = next(f for f in files if f.rel == "a.log")
        self.assertEqual(primary.aliases, [])

    def test_follow_symlink_directory(self):
        target = Path(self._tmp.name) / "target"
        target.mkdir()
        (target / "t.log").write_bytes(b"t\n")
        (self.root / "linked").symlink_to(target, target_is_directory=True)
        files = discover(
            self.root,
            self.config(follow_symlinks=False),
            None,
            "path",
        )
        self.assertNotIn("linked/t.log", [f.rel for f in files])
        files = discover(
            self.root,
            self.config(follow_symlinks=True),
            None,
            "path",
        )
        self.assertIn("linked/t.log", [f.rel for f in files])


if __name__ == "__main__":
    unittest.main()
