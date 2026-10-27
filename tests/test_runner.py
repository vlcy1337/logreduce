from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from logreduce.models import SourceFile
from logreduce.runner import _find_split_offset, build_batches


def source(name: str, size: int) -> SourceFile:
    return SourceFile(path=Path(name), rel=name, size=size, mtime_ns=0)


class BatchTests(unittest.TestCase):
    def test_batches_are_contiguous_and_bounded(self):
        files = [source(f"f{i}", i + 1) for i in range(10)]
        batches = build_batches(files, 3)
        self.assertLessEqual(len(batches), 3)
        self.assertEqual([item for batch in batches for item in batch], files)
        self.assertTrue(all(batch for batch in batches))

    def test_single_batch_for_one_job(self):
        files = [source("a", 10), source("b", 10)]
        self.assertEqual(build_batches(files, 1), [files])

    def test_more_jobs_than_files(self):
        files = [source("a", 10), source("b", 10)]
        batches = build_batches(files, 8)
        self.assertEqual([item for batch in batches for item in batch], files)


class SplitOffsetTests(unittest.TestCase):
    def test_split_at_newline_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x"
            path.write_bytes(b"aaa\nbbb\nccc\n")
            boundary = _find_split_offset(path, 0, 12)
            self.assertIsNotNone(boundary)
            assert boundary is not None
            self.assertTrue(0 < boundary < 12)
            self.assertEqual(path.read_bytes()[boundary - 1], 10)

    def test_no_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x"
            path.write_bytes(b"a" * 100)
            self.assertIsNone(_find_split_offset(path, 0, 100))

    def test_subrange(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x"
            path.write_bytes(b"aaa\nbbb\nccc\n")
            boundary = _find_split_offset(path, 4, 9)
            self.assertEqual(boundary, 8)

    def test_newline_at_end_is_not_a_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x"
            path.write_bytes(b"aaaa\n")
            self.assertIsNone(_find_split_offset(path, 0, 5))


if __name__ == "__main__":
    unittest.main()
