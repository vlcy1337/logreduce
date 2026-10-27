#!/usr/bin/env python3
"""Synthetic benchmark for logReduce.

Generates a temporary log file, then runs the CLI for literal and regex
replacement configurations with different worker counts.  The tool is invoked
as a subprocess so process startup and pool setup are included in the timing.

Usage:
    python3 bench/bench.py
    python3 bench/bench.py --lines 5000000 --jobs 12
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def build_config(path: Path, output: Path, rules: str, jobs: int) -> None:
    path.write_text(
        "\n".join(
            [
                "version = 1",
                "[input]",
                'include = ["**/*.log"]',
                "[output]",
                f"path = {json.dumps(str(output))}",
                "delete_sources = false",
                "[engine]",
                'mode = "direct"',
                f"jobs = {jobs}",
                'chunk_size = "8MiB"',
                "fsync = false",
                rules,
                "",
            ]
        ),
        encoding="utf-8",
    )


def run_case(
    logs: Path,
    config: Path,
    output: Path,
) -> float:
    if output.exists():
        output.unlink()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    started = time.perf_counter()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "logreduce",
            "-d",
            str(logs),
            "-c",
            str(config),
            "--force",
            "--keep-sources",
            "--quiet",
        ],
        check=True,
        env=env,
    )
    elapsed = time.perf_counter() - started
    return elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lines", type=int, default=1_000_000)
    parser.add_argument("--jobs", type=int, default=0, help="0 = CPU 核数")
    parser.add_argument("--dir", type=Path, default=None)
    args = parser.parse_args()

    jobs = args.jobs or (os.cpu_count() or 1)
    tmp_ctx = None
    if args.dir is None:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="logreduce-bench-")
        root = Path(tmp_ctx.name)
    else:
        root = args.dir
        root.mkdir(parents=True, exist_ok=True)

    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / "app.log"
    line = (
        b"2026-10-28 INFO module=api user=alice token=abc123 "
        b"host=old-host msg=hello world\n"
    )
    with log_path.open("wb") as handle:
        for _ in range(args.lines):
            handle.write(line)
    input_bytes = log_path.stat().st_size

    literal_rules = "\n".join(
        [
            "[[rules]]",
            'name = "host"',
            'action = "replace"',
            'match_type = "literal"',
            'pattern = "old-host"',
            'replacement = "new-host"',
            "[[rules]]",
            'name = "token"',
            'action = "replace"',
            'match_type = "literal"',
            'pattern = "token=abc123"',
            'replacement = "token=***"',
        ]
    )
    regex_rules = "\n".join(
        [
            "[[rules]]",
            'name = "token"',
            'action = "replace"',
            'match_type = "regex"',
            "pattern = '(token=)[A-Za-z0-9._-]+'",
            "replacement = '\\1***'",
            "[[rules]]",
            'name = "user"',
            'action = "replace"',
            'match_type = "regex"',
            "pattern = 'user=alice'",
            "replacement = 'user=bob'",
        ]
    )

    for label, rules in (("literal", literal_rules), ("regex", regex_rules)):
        for count in sorted({1, jobs}):
            config = root / f"{label}-{count}.toml"
            output = root / f"{label}-{count}.out.log"
            build_config(config, output, rules, count)
            elapsed = run_case(logs, config, output)
            mib = input_bytes / (1024 * 1024)
            print(
                f"{label:8s} jobs={count:<3d} "
                f"{elapsed:8.3f} s  {mib / elapsed:8.1f} MiB/s"
            )

    print(f"\ninput: {input_bytes / (1024 * 1024):.1f} MiB, lines: {args.lines:,}")
    if tmp_ctx is not None:
        tmp_ctx.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
