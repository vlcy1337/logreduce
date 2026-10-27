from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from logreduce.cli import main


def toml_literal(value: str) -> str:
    if "'" in value:
        raise ValueError("test config helper does not support single quotes")
    return "'" + value + "'"


def config_text(
    output: Path,
    rules,
    *,
    mode: str = "inplace",
    jobs: int = 1,
    delete_sources: bool = True,
    separator: str = "\n",
    include=("**/*.log",),
    fsync: bool = False,
    sort: str = "path",
) -> str:
    include_text = ", ".join(json.dumps(item) for item in include)
    lines = [
        "version = 1",
        "[input]",
        f"include = [{include_text}]",
        "[output]",
        f"path = {json.dumps(str(output))}",
        f"separator = {json.dumps(separator)}",
        f"delete_sources = {'true' if delete_sources else 'false'}",
        "[engine]",
        f"mode = {json.dumps(mode)}",
        f"jobs = {jobs}",
        'chunk_size = "4KiB"',
        f"fsync = {'true' if fsync else 'false'}",
        f"sort = {json.dumps(sort)}",
    ]
    for rule in rules:
        lines.append("[[rules]]")
        for key, value in rule.items():
            if key in ("match_type", "action", "flags", "when_type"):
                lines.append(f"{key} = {json.dumps(value)}")
            else:
                lines.append(f"{key} = {toml_literal(value)}")
    return "\n".join(lines) + "\n"


def run_cli(args):
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(list(args))
        except SystemExit as exc:
            code = int(exc.code) if exc.code is not None else 0
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.output = self.root / "merged.log"
        self.config = self.root / "logreduce.toml"

    def tearDown(self):
        self._tmp.cleanup()

    def write_config(self, rules, **kwargs):
        self.config.write_text(
            config_text(self.output, rules, **kwargs), encoding="utf-8"
        )

    def write_log(self, rel: str, data: bytes) -> Path:
        path = self.logs / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_inplace_end_to_end(self):
        self.write_log("b.log", b"INFO b old\nDEBUG noise\nERROR token=abc old\n")
        self.write_log("a.log", b"INFO a old\n")
        self.write_config(
            [
                {
                    "action": "drop",
                    "match_type": "regex",
                    "pattern": r"^\s*DEBUG\b",
                },
                {
                    "action": "replace",
                    "match_type": "regex",
                    "pattern": r"(token=)[A-Za-z0-9._-]+",
                    "replacement": r"\1***",
                },
                {
                    "action": "replace",
                    "match_type": "literal",
                    "pattern": "old",
                    "replacement": "new",
                },
            ]
        )
        code, out, err = run_cli(
            ["-d", str(self.logs), "-c", str(self.config), "--stats"]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            self.output.read_bytes(),
            b"INFO a new\nINFO b new\nERROR token=*** new\n",
        )
        self.assertFalse((self.logs / "a.log").exists())
        self.assertFalse((self.logs / "b.log").exists())
        self.assertIn("处理完成", out)
        self.assertIn("丢弃: 1 行", out)

    def test_direct_end_to_end(self):
        self.write_log("a.log", b"old\n")
        self.write_config(
            [
                {
                    "action": "replace",
                    "match_type": "literal",
                    "pattern": "old",
                    "replacement": "new",
                }
            ],
            mode="direct",
        )
        code, _, err = run_cli(
            ["-d", str(self.logs), "-c", str(self.config), "--quiet"]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.output.read_bytes(), b"new\n")
        self.assertFalse((self.logs / "a.log").exists())

    def test_parallel_equivalence(self):
        def build(root: Path, jobs: int) -> bytes:
            logs = root / "logs"
            logs.mkdir(parents=True)
            for i in range(4):
                (logs / f"f{i}.log").write_bytes(
                    f"x{i}\n".encode() * 50 + b"tail"
                )
            output = root / "merged.log"
            config = root / "c.toml"
            config.write_text(
                config_text(
                    output,
                    [
                        {
                            "action": "replace",
                            "match_type": "literal",
                            "pattern": "x",
                            "replacement": "y",
                        }
                    ],
                    mode="direct",
                    jobs=jobs,
                ),
                encoding="utf-8",
            )
            code, _, err = run_cli(
                ["-d", str(logs), "-c", str(config), "--quiet"]
            )
            self.assertEqual(code, 0, err)
            return output.read_bytes()

        with tempfile.TemporaryDirectory() as tmp:
            root1 = Path(tmp) / "one"
            root2 = Path(tmp) / "two"
            root1.mkdir()
            root2.mkdir()
            self.assertEqual(build(root1, 1), build(root2, 2))

    def test_keep_sources_inplace(self):
        path = self.write_log("a.log", b"old\n")
        self.write_config(
            [
                {
                    "action": "replace",
                    "match_type": "literal",
                    "pattern": "old",
                    "replacement": "new",
                }
            ]
        )
        code, _, err = run_cli(
            [
                "-d",
                str(self.logs),
                "-c",
                str(self.config),
                "--keep-sources",
                "--quiet",
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(path.read_bytes(), b"new\n")
        self.assertEqual(self.output.read_bytes(), b"new\n")

    def test_keep_sources_direct(self):
        path = self.write_log("a.log", b"old\n")
        self.write_config(
            [
                {
                    "action": "replace",
                    "match_type": "literal",
                    "pattern": "old",
                    "replacement": "new",
                }
            ],
            mode="direct",
        )
        code, _, err = run_cli(
            [
                "-d",
                str(self.logs),
                "-c",
                str(self.config),
                "--keep-sources",
                "--quiet",
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(path.read_bytes(), b"old\n")
        self.assertEqual(self.output.read_bytes(), b"new\n")

    def test_dry_run(self):
        path = self.write_log("a.log", b"old\n")
        self.write_config(
            [
                {
                    "action": "replace",
                    "match_type": "literal",
                    "pattern": "old",
                    "replacement": "new",
                }
            ]
        )
        code, out, err = run_cli(
            ["-d", str(self.logs), "-c", str(self.config), "--dry-run"]
        )
        self.assertEqual(code, 0, err)
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), b"old\n")
        self.assertFalse(self.output.exists())
        self.assertIn("a.log", out)
        self.assertIn("dry-run", out)

    def test_output_exists_requires_force(self):
        path = self.write_log("a.log", b"old\n")
        self.output.write_bytes(b"existing\n")
        self.write_config(
            [
                {
                    "action": "replace",
                    "match_type": "literal",
                    "pattern": "old",
                    "replacement": "new",
                }
            ]
        )
        code, _, err = run_cli(
            ["-d", str(self.logs), "-c", str(self.config), "--quiet"]
        )
        self.assertEqual(code, 1)
        self.assertIn("--force", err)
        self.assertEqual(self.output.read_bytes(), b"existing\n")
        self.assertTrue(path.exists())

        code, _, err = run_cli(
            [
                "-d",
                str(self.logs),
                "-c",
                str(self.config),
                "--force",
                "--quiet",
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.output.read_bytes(), b"new\n")
        self.assertFalse(path.exists())

    def test_validate(self):
        self.write_config([{"action": "drop", "pattern": "x"}])
        code, out, err = run_cli(["--validate", "-c", str(self.config)])
        self.assertEqual(code, 0, err)
        self.assertIn("配置有效", out)

        code, out, _ = run_cli(
            ["--validate", "-c", str(self.config), "--quiet"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_invalid_config_exit_2(self):
        self.config.write_text("version = 1\n", encoding="utf-8")
        code, _, err = run_cli(
            ["-d", str(self.logs), "-c", str(self.config), "--quiet"]
        )
        self.assertEqual(code, 2)
        self.assertIn("配置错误", err)

    def test_stats_json(self):
        self.write_log("a.log", b"x\n")
        self.write_config(
            [
                {
                    "action": "replace",
                    "match_type": "literal",
                    "pattern": "x",
                    "replacement": "y",
                }
            ]
        )
        code, out, err = run_cli(
            [
                "-d",
                str(self.logs),
                "-c",
                str(self.config),
                "--stats-json",
            ]
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["files_processed"], 1)
        self.assertEqual(payload["bytes_in"], 2)
        self.assertEqual(payload["bytes_out"], 2)
        self.assertEqual(payload["lines_in"], 1)
        self.assertEqual(payload["lines_out"], 1)
        self.assertEqual(payload["mode"], "inplace")

    def test_output_inside_log_dir(self):
        self.write_log("a.log", b"old\n")
        self.output = self.logs / "merged.log"
        self.write_config(
            [
                {
                    "action": "replace",
                    "match_type": "literal",
                    "pattern": "old",
                    "replacement": "new",
                }
            ]
        )
        code, _, err = run_cli(
            ["-d", str(self.logs), "-c", str(self.config), "--quiet"]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.output.read_bytes(), b"new\n")
        self.assertFalse((self.logs / "a.log").exists())

    def test_separator_between_files_without_newline(self):
        self.write_log("a.log", b"a")
        self.write_log("b.log", b"b")
        self.write_config(
            [{"action": "replace", "match_type": "literal", "pattern": "z", "replacement": "q"}]
        )
        code, _, err = run_cli(
            ["-d", str(self.logs), "-c", str(self.config), "--quiet"]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.output.read_bytes(), b"a\nb")

    def test_all_lines_dropped(self):
        self.write_log("a.log", b"x\ny\n")
        self.write_config(
            [{"action": "drop", "match_type": "regex", "pattern": r".*"}]
        )
        code, _, err = run_cli(
            ["-d", str(self.logs), "-c", str(self.config), "--quiet"]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.output.read_bytes(), b"")
        self.assertFalse((self.logs / "a.log").exists())

    def test_mode_override(self):
        self.write_log("a.log", b"x\n")
        self.write_config(
            [
                {
                    "action": "replace",
                    "match_type": "literal",
                    "pattern": "x",
                    "replacement": "y",
                }
            ],
            mode="inplace",
        )
        code, _, err = run_cli(
            [
                "-d",
                str(self.logs),
                "-c",
                str(self.config),
                "--mode",
                "direct",
                "--quiet",
            ]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.output.read_bytes(), b"y\n")
        self.assertFalse((self.logs / "a.log").exists())

    def test_no_files(self):
        self.write_config([{"action": "drop", "pattern": "x"}])
        code, _, err = run_cli(
            ["-d", str(self.logs), "-c", str(self.config), "--quiet"]
        )
        self.assertEqual(code, 1)
        self.assertIn("没有匹配的输入文件", err)

    def test_output_same_as_config(self):
        self.write_log("a.log", b"x\n")
        self.output = self.config
        self.write_config([{"action": "drop", "pattern": "x"}])
        code, _, err = run_cli(
            ["-d", str(self.logs), "-c", str(self.config), "--quiet"]
        )
        self.assertEqual(code, 2)
        self.assertIn("配置文件", err)

    def test_output_hardlink_config_rejected(self):
        self.write_config([{"action": "drop", "pattern": "x"}])
        original = self.config.read_bytes()
        output = self.root / "out.log"
        os.link(self.config, output)
        code, _, err = run_cli(
            [
                "-d",
                str(self.logs),
                "-c",
                str(self.config),
                "-o",
                str(output),
                "--force",
                "--quiet",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("配置文件", err)
        self.assertEqual(self.config.read_bytes(), original)

    def test_config_file_inside_log_dir_is_skipped(self):
        self.write_log("a.log", b"old\n")
        config = self.logs / "config.toml"
        config.write_text(
            config_text(
                self.output,
                [
                    {
                        "action": "replace",
                        "match_type": "literal",
                        "pattern": "old",
                        "replacement": "new",
                    }
                ],
                include=("**/*",),
            ),
            encoding="utf-8",
        )
        code, _, err = run_cli(
            ["-d", str(self.logs), "-c", str(config), "--quiet"]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.output.read_bytes(), b"new\n")
        self.assertTrue(config.exists())
        self.assertFalse((self.logs / "a.log").exists())

    def test_hardlink(self):
        primary = self.write_log("a.log", b"old\n")
        alias = self.logs / "alias.log"
        os.link(primary, alias)
        self.write_config(
            [
                {
                    "action": "replace",
                    "match_type": "literal",
                    "pattern": "old",
                    "replacement": "new",
                }
            ],
            mode="direct",
        )
        code, _, err = run_cli(
            ["-d", str(self.logs), "-c", str(self.config), "--quiet"]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.output.read_bytes(), b"new\n")
        self.assertFalse(primary.exists())
        self.assertFalse(alias.exists())

    def test_quiet(self):
        self.write_log("a.log", b"x\n")
        self.write_config(
            [
                {
                    "action": "replace",
                    "match_type": "literal",
                    "pattern": "x",
                    "replacement": "y",
                }
            ]
        )
        code, out, err = run_cli(
            ["-d", str(self.logs), "-c", str(self.config), "--quiet"]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_direct_chunk_parallel_with_filters(self):
        import logreduce.runner as runner_module

        old_threshold = runner_module.CHUNK_PARALLEL_THRESHOLD
        runner_module.CHUNK_PARALLEL_THRESHOLD = 64
        try:
            data = b"".join(
                (b"KEEP line %04d\n" % i)
                if i % 2 == 0
                else (b"DROP line %04d\n" % i)
                for i in range(500)
            )
            self.write_log("big.log", data)
            self.write_config(
                [
                    {
                        "action": "drop",
                        "match_type": "regex",
                        "pattern": r"^DROP\b",
                    }
                ],
                mode="direct",
                jobs=4,
            )
            code, out, err = run_cli(
                [
                    "-d",
                    str(self.logs),
                    "-c",
                    str(self.config),
                    "--stats-json",
                ]
            )
            self.assertEqual(code, 0, err)
            expected = b"".join(
                (b"KEEP line %04d\n" % i) for i in range(0, 500, 2)
            )
            self.assertEqual(self.output.read_bytes(), expected)
            payload = json.loads(out)
            self.assertEqual(payload["files_processed"], 1)
            self.assertEqual(payload["lines_in"], 500)
            self.assertEqual(payload["lines_out"], 250)
            self.assertEqual(payload["lines_dropped"], 250)
        finally:
            runner_module.CHUNK_PARALLEL_THRESHOLD = old_threshold

    def test_direct_chunk_parallel(self):
        import logreduce.runner as runner_module

        old_threshold = runner_module.CHUNK_PARALLEL_THRESHOLD
        runner_module.CHUNK_PARALLEL_THRESHOLD = 64
        try:
            data = b"".join(
                f"line {i:04d} x token=abc\n".encode() for i in range(500)
            )
            self.write_log("big.log", data)
            self.write_config(
                [
                    {
                        "action": "replace",
                        "match_type": "regex",
                        "pattern": r"(token=)[A-Za-z0-9._-]+",
                        "replacement": r"\1***",
                    },
                    {
                        "action": "replace",
                        "match_type": "literal",
                        "pattern": " x ",
                        "replacement": " y ",
                    },
                ],
                mode="direct",
                jobs=4,
            )
            code, out, err = run_cli(
                [
                    "-d",
                    str(self.logs),
                    "-c",
                    str(self.config),
                    "--stats-json",
                ]
            )
            self.assertEqual(code, 0, err)
            expected = data.replace(b"token=abc", b"token=***").replace(
                b" x ", b" y "
            )
            self.assertEqual(self.output.read_bytes(), expected)
            payload = json.loads(out)
            self.assertEqual(payload["files_processed"], 1)
            self.assertEqual(payload["lines_in"], 500)
            self.assertEqual(payload["lines_out"], 500)
            self.assertFalse((self.logs / "big.log").exists())
        finally:
            runner_module.CHUNK_PARALLEL_THRESHOLD = old_threshold


if __name__ == "__main__":
    unittest.main()
