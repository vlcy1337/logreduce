from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from logreduce.config import compile_rules, parse_config
from logreduce.engine import ProcessingError, execute_tasks
from logreduce.files import FileRange, SourceFile, WorkTask, build_work_tasks


def _source(path: Path, relative_path: str | None = None) -> SourceFile:
    stat_result = path.stat()
    return SourceFile(
        path=path,
        relative_path=relative_path or path.name,
        size=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
        inode=stat_result.st_ino,
        device=stat_result.st_dev,
    )


class EngineTests(unittest.TestCase):
    def test_ordered_pipeline_and_line_endings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "one.log"
            source.write_bytes(
                b"INFO id=1\r\nERROR id=22\r\nERROR id=333"
            )
            fragments = root / "fragments"
            config = parse_config(
                {
                    "version": 1,
                    "output": "out.log",
                    "rules": [
                        {
                            "filter": {"mode": "include", "pattern": "ERROR"},
                            "replace": {
                                "pattern": r"id=(\d+)",
                                "replacement": r"id=<\1>",
                            },
                        },
                        {
                            "replace": {
                                "pattern": r"<(\d+)>",
                                "replacement": r"<\1-seen>",
                            }
                        },
                    ],
                }
            )
            tasks = build_work_tasks([_source(source)], config.encoding)
            result = execute_tasks(
                tasks,
                fragments,
                workers=1,
                rules=compile_rules(config.rules),
                encoding=config.encoding,
            )

            output = b"".join(
                item.read_bytes() for item in sorted(fragments.glob("*.part"))
            )
            self.assertEqual(
                output,
                b"ERROR id=<22-seen>\r\nERROR id=<333-seen>",
            )
            self.assertEqual(result.output_bytes, len(output))

    def test_no_rules_is_byte_exact_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "binary.log"
            content = b"\xff\x00ERROR\r\nno-newline"
            source.write_bytes(content)
            fragments = root / "fragments"
            tasks = build_work_tasks([_source(source)], "utf-8")
            execute_tasks(tasks, fragments, 1, (), "utf-8")

            output = (fragments / "000000000000.part").read_bytes()
            self.assertEqual(output, content)

    def test_multiple_ranges_preserve_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "one.log"
            source.write_bytes(b"first\nsecond\nthird")
            config = parse_config(
                {
                    "version": 1,
                    "output": "out.log",
                    "rules": [
                        {"filter": {"mode": "include", "pattern": "."}}
                    ],
                }
            )
            task = WorkTask(
                index=0,
                ranges=(
                    FileRange(source, "one.log", 0, 6),
                    FileRange(source, "one.log", 6, 12),
                ),
                input_bytes=18,
            )
            fragments = root / "fragments"
            execute_tasks(
                [task],
                fragments,
                2,
                compile_rules(config.rules),
                config.encoding,
            )
            self.assertEqual(
                (fragments / "000000000000.part").read_bytes(),
                b"first\nsecond\nthird",
            )

    def test_invalid_bytes_abort_processing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "one.log"
            source.write_bytes(b"bad:\xff\n")
            config = parse_config(
                {
                    "version": 1,
                    "output": "out.log",
                    "rules": [
                        {"filter": {"mode": "include", "pattern": "bad"}}
                    ],
                }
            )
            with self.assertRaises(ProcessingError):
                execute_tasks(
                    build_work_tasks([_source(source)], "utf-8"),
                    root / "fragments",
                    1,
                    compile_rules(config.rules),
                    "utf-8",
                )

    def test_filter_only_preserves_cr_and_missing_final_newline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "one.log"
            source.write_bytes(
                b"ERROR keep\r\nINFO drop\rmore\rERROR end"
            )
            config = parse_config(
                {
                    "version": 1,
                    "output": "out.log",
                    "rules": [
                        {"filter": {"mode": "include", "pattern": "ERROR"}}
                    ],
                }
            )
            fragments = root / "fragments"
            execute_tasks(
                build_work_tasks([_source(source)], config.encoding),
                fragments,
                1,
                compile_rules(config.rules),
                config.encoding,
            )
            self.assertEqual(
                (fragments / "000000000000.part").read_bytes(),
                b"ERROR keep\r\nERROR end",
            )

    def test_large_file_is_split_at_line_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "large.log"
            expected = b"".join(
                f"ERROR keep {index}\n".encode() for index in range(20)
            )
            source.write_bytes(expected)
            config = parse_config(
                {
                    "version": 1,
                    "output": "out.log",
                    "rules": [
                        {"filter": {"mode": "include", "pattern": "ERROR"}}
                    ],
                }
            )
            with mock.patch("logreduce.files.TASK_TARGET_BYTES", 32):
                tasks = build_work_tasks([_source(source)], config.encoding)
            self.assertGreater(len(tasks), 1)
            fragments = root / "fragments"
            execute_tasks(
                tasks,
                fragments,
                2,
                compile_rules(config.rules),
                config.encoding,
            )
            output = b"".join(
                part.read_bytes() for part in sorted(fragments.glob("*.part"))
            )
            self.assertEqual(output, expected)

    def test_filter_does_not_join_lines_across_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "a.log"
            second = root / "b.log"
            first.write_bytes(b"prefix-without-newline")
            second.write_bytes(b"ERROR second-file")
            config = parse_config(
                {
                    "version": 1,
                    "output": "out.log",
                    "rules": [
                        {"filter": {"mode": "include", "pattern": "ERROR"}}
                    ],
                }
            )
            sources = [_source(first), _source(second)]
            tasks = build_work_tasks(sources, config.encoding)
            fragments = root / "fragments"
            execute_tasks(
                tasks,
                fragments,
                1,
                compile_rules(config.rules),
                config.encoding,
            )
            output = b"".join(
                part.read_bytes() for part in sorted(fragments.glob("*.part"))
            )
            self.assertEqual(output, b"ERROR second-file")


if __name__ == "__main__":
    unittest.main()
