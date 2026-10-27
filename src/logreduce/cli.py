from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import ConfigError
from .engine import ProcessingError
from .files import FileSystemError, LockError
from .runner import ProgressEvent, run_processing
from .transaction import TransactionError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="logreduce",
        description=(
            "Apply an ordered line-filtering pipeline to every regular file in "
            "a log directory, concatenate the results, and remove the sources."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "--log-dir",
        required=True,
        metavar="PATH",
        help="directory containing log files",
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="PATH",
        help="TOML processing configuration",
    )
    parser.add_argument(
        "--workers",
        type=_positive_integer,
        default=None,
        metavar="N",
        help="worker process count (default: available CPU count)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing output file transactionally",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate configuration and enumerate work without writing files",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print processing details to stderr",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return _run(args)
    except ConfigError as exc:
        print(f"logreduce: configuration error: {exc}", file=sys.stderr)
        return 2
    except LockError as exc:
        print(f"logreduce: {exc}", file=sys.stderr)
        return 1
    except (FileSystemError, ProcessingError, TransactionError) as exc:
        print(f"logreduce: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"logreduce: filesystem error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "logreduce: interrupted; restart to recover any interrupted "
            "transaction",
            file=sys.stderr,
        )
        return 1


def _run(args: argparse.Namespace) -> int:
    def show_progress(event: ProgressEvent) -> None:
        if not args.verbose:
            return
        if event.phase == "recovered":
            print(event.message, file=sys.stderr)
        elif event.phase == "selected":
            print(event.message, file=sys.stderr)

    summary = run_processing(
        log_dir=args.log_dir,
        config_path=args.config,
        workers=args.workers,
        force=args.force,
        dry_run=args.dry_run,
        progress=show_progress,
    )
    if args.dry_run:
        print(
            f"Would process {summary.file_count} file(s), "
            f"{summary.input_bytes} byte(s); output: {summary.output_path}"
        )
    else:
        print(
            f"Processed {summary.file_count} file(s); "
            f"input {summary.input_bytes} byte(s); "
            f"output {summary.output_bytes} byte(s); "
            f"output: {summary.output_path}"
        )
    return 0


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed
