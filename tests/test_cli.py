from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from logreduce.cli import main


def _run_main(arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = main(arguments)
    return result, stdout.getvalue(), stderr.getvalue()


class CliTests(unittest.TestCase):
    def _config(self, root: Path, body: str) -> Path:
        path = root / "config.toml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_recursive_order_replace_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "nested").mkdir()
            (root / "b.log").write_bytes(
                b"WARN id=2\r\nINFO id=9\r\n"
            )
            (root / "a.log").write_bytes(b"ERROR id=1\n")
            (root / "nested" / "c.log").write_bytes(b"ERROR id=3")
            config = self._config(
                root,
                """\
version = 1
output = "combined.log"

[[rules]]
name = "select"
filter = { mode = "include", pattern = '^(ERROR|WARN)' }
replace = { pattern = 'id=(\\d+)', replacement = 'id=<\\1>' }
""",
            )

            code, stdout, stderr = _run_main(
                [
                    "--log-dir",
                    str(root),
                    "--config",
                    str(config),
                    "--workers",
                    "1",
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Processed 3 file(s)", stdout)
            self.assertEqual(
                (root / "combined.log").read_bytes(),
                b"ERROR id=<1>\nWARN id=<2>\r\nERROR id=<3>",
            )
            self.assertEqual(
                {path.name for path in root.iterdir() if path.is_file()},
                {"combined.log", "config.toml"},
            )
            self.assertTrue((root / "nested").is_dir())

    def test_empty_rules_merge_files_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "b.log").write_bytes(b"B\r\n")
            (root / "a.log").write_bytes(b"A\n")
            config = self._config(
                root,
                'version = 1\noutput = "merged.log"\n',
            )
            code, _, stderr = _run_main(
                [
                    "--log-dir",
                    str(root),
                    "--config",
                    str(config),
                    "--workers",
                    "1",
                ]
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual((root / "merged.log").read_bytes(), b"A\nB\r\n")
            self.assertFalse((root / "a.log").exists())
            self.assertFalse((root / "b.log").exists())

    def test_dry_run_does_not_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.log").write_bytes(b"ERROR\n")
            config = self._config(
                root,
                """\
version = 1
output = "out.log"
[[rules]]
filter = { mode = "include", pattern = "ERROR" }
""",
            )
            code, stdout, stderr = _run_main(
                [
                    "--log-dir",
                    str(root),
                    "--config",
                    str(config),
                    "--dry-run",
                    "--workers",
                    "1",
                ]
            )
            self.assertEqual(code, 0, stderr)
            self.assertIn("Would process 1 file(s)", stdout)
            self.assertTrue((root / "a.log").exists())
            self.assertFalse((root / "out.log").exists())

    def test_existing_output_requires_force(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.log").write_bytes(b"old\n")
            (root / "out.log").write_bytes(b"previous")
            config = self._config(
                root,
                'version = 1\noutput = "out.log"\n',
            )
            common = [
                "--log-dir",
                str(root),
                "--config",
                str(config),
                "--workers",
                "1",
            ]
            code, _, stderr = _run_main(common)
            self.assertEqual(code, 1)
            self.assertIn("--force", stderr)
            self.assertEqual((root / "out.log").read_bytes(), b"previous")
            self.assertTrue((root / "a.log").exists())

            code, _, stderr = _run_main([*common, "--force"])
            self.assertEqual(code, 0, stderr)
            self.assertEqual((root / "out.log").read_bytes(), b"old\n")
            self.assertFalse((root / "a.log").exists())

    def test_decode_error_preserves_every_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.log").write_bytes(b"valid\n")
            (root / "b.log").write_bytes(b"invalid:\xff\n")
            config = self._config(
                root,
                """\
version = 1
output = "out.log"
[[rules]]
filter = { mode = "include", pattern = "valid" }
""",
            )
            code, _, _ = _run_main(
                [
                    "--log-dir",
                    str(root),
                    "--config",
                    str(config),
                    "--workers",
                    "2",
                ]
            )
            self.assertEqual(code, 1)
            self.assertTrue((root / "a.log").exists())
            self.assertTrue((root / "b.log").exists())
            self.assertFalse((root / "out.log").exists())
            self.assertFalse(
                any(path.name.startswith(".logreduce-txn-") for path in root.iterdir())
            )

    def test_configuration_error_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._config(root, "version = 2\noutput = 'out.log'\n")
            code, _, stderr = _run_main(
                ["--log-dir", str(root), "--config", str(config), "--dry-run"]
            )
            self.assertEqual(code, 2)
            self.assertIn("configuration error", stderr)

    def test_symlinks_are_not_processed_or_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.log"
            target.write_bytes(b"TARGET\n")
            link = root / "link.log"
            link.symlink_to(target)
            config = self._config(
                root,
                'version = 1\noutput = "out.log"\n',
            )

            code, _, stderr = _run_main(
                [
                    "--log-dir",
                    str(root),
                    "--config",
                    str(config),
                    "--workers",
                    "1",
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertTrue(link.is_symlink())
            self.assertFalse(target.exists())
            self.assertEqual((root / "out.log").read_bytes(), b"TARGET\n")


if __name__ == "__main__":
    unittest.main()
