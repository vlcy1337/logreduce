from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


_LINE = (
    b"2026-10-28T12:34:56.123Z service=api level=INFO "
    b"request_id=0123456789abcdef operation=lookup user=alice "
    b"elapsed_ms=12 message=request completed successfully\n"
)
_ONE_RULE = """\
version = 1
output = "combined.log"
[[rules]]
filter = { mode = "include", pattern = "ERROR" }
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Local LogReduce benchmark")
    parser.add_argument("--size-mib", type=int, default=512)
    parser.add_argument("--files", type=int, default=16)
    parser.add_argument("--rules", type=int, default=10)
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args()
    if (
        args.size_mib < 1
        or args.files < 1
        or args.rules < 1
        or args.workers < 1
    ):
        parser.error("sizes must be positive")

    source_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    python_path = str(source_root / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        item
        for item in (python_path, environment.get("PYTHONPATH", ""))
        if item
    )

    with tempfile.TemporaryDirectory(prefix="logreduce-bench-") as temporary:
        base = Path(temporary)
        grep_root = base / "grep"
        single_root = base / "single"
        parallel_root = base / "parallel"
        multi_root = base / "multi"
        _make_corpus(grep_root, args.size_mib, args.files)
        _make_corpus(single_root, args.size_mib, args.files)
        _make_corpus(parallel_root, args.size_mib, args.files)
        _make_corpus(multi_root, args.size_mib, args.files)

        grep_seconds = _time_grep(grep_root, base / "grep-output.log")
        single_worker_seconds = _time_logreduce(
            single_root,
            _write_config(single_root, 1),
            workers=1,
            environment=environment,
        )
        single_seconds = _time_logreduce(
            parallel_root,
            _write_config(single_root, 1),
            workers=args.workers,
            environment=environment,
        )
        multi_seconds = _time_logreduce(
            multi_root,
            _write_config(multi_root, args.rules),
            workers=args.workers,
            environment=environment,
        )

        mib = float(args.size_mib)
        print(f"grep baseline:          {mib / grep_seconds:8.1f} MiB/s")
        print(
            f"logreduce, 1 rule, 1w:  {mib / single_worker_seconds:8.1f} MiB/s "
            f"({single_worker_seconds:.3f}s)"
        )
        print(
            f"logreduce, 1 rule, all: {mib / single_seconds:8.1f} MiB/s "
            f"({single_seconds:.3f}s)"
        )
        print(
            f"logreduce, {args.rules} rules:    {mib / multi_seconds:8.1f} MiB/s "
            f"({multi_seconds:.3f}s)"
        )
        print(f"one-rule grep ratio:    {grep_seconds / single_seconds:8.2f}x")
    return 0


def _make_corpus(root: Path, size_mib: int, file_count: int) -> None:
    root.mkdir()
    target_bytes = size_mib * 1024 * 1024
    bytes_per_file = max(len(_LINE), target_bytes // file_count)
    error_line = _LINE.replace(b"level=INFO", b"level=ERROR")
    for file_index in range(file_count):
        path = root / f"{file_index:04d}.log"
        with path.open("wb") as output:
            written = 0
            line_index = 0
            while written < bytes_per_file:
                line = error_line if line_index % 10 == 0 else _LINE
                output.write(line)
                written += len(line)
                line_index += 1


def _write_config(root: Path, rule_count: int) -> Path:
    path = root / "config.toml"
    body = _ONE_RULE
    for index in range(2, rule_count + 1):
        body += (
            "\n[[rules]]\n"
            f"name = 'noop {index}'\n"
            "replace = { pattern = '^$', replacement = '' }\n"
        )
    path.write_text(body, encoding="utf-8")
    return path


def _time_grep(root: Path, output: Path) -> float:
    files = [str(path) for path in sorted(root.glob("*.log"))]
    started = time.perf_counter()
    with output.open("wb") as destination:
        subprocess.run(
            ["grep", "-E", "ERROR", *files],
            stdout=destination,
            check=True,
        )
    return time.perf_counter() - started


def _time_logreduce(
    root: Path,
    config: Path,
    workers: int,
    environment: dict[str, str],
) -> float:
    started = time.perf_counter()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "logreduce",
            "--log-dir",
            str(root),
            "--config",
            str(config),
            "--workers",
            str(workers),
        ],
        check=True,
        env=environment,
        stdout=subprocess.DEVNULL,
    )
    return time.perf_counter() - started


if __name__ == "__main__":
    raise SystemExit(main())
