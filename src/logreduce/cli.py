from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, compile_rules, load_config
from .engine import (
    ProcessingError,
    available_workers,
    execute_tasks,
    merge_fragments,
)
from .files import (
    DirectoryLock,
    FileSystemError,
    LockError,
    build_work_tasks,
    discover_sources,
    resolve_config_path,
    resolve_log_directory,
    resolve_output_path,
    verify_sources_unchanged,
)
from .transaction import (
    Transaction,
    TransactionError,
    pending_transaction_directories,
    recover_pending_transactions,
)


_MERGE_SPACE_MARGIN = 64 * 1024 * 1024


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
    root = resolve_log_directory(args.log_dir)
    config_path = resolve_config_path(args.config)
    config = load_config(config_path)
    output = resolve_output_path(root, config.output)
    if output == config_path:
        raise ConfigError("output path must not overwrite the configuration file")

    workers = args.workers or available_workers()
    rules = compile_rules(config.rules)

    with DirectoryLock(root):
        if args.dry_run:
            pending = pending_transaction_directories(root)
            if pending:
                raise TransactionError(
                    "an interrupted transaction requires recovery; run without "
                    "--dry-run first"
                )
        recovered = recover_pending_transactions(root)
        if recovered and args.verbose:
            print(
                f"Recovered {len(recovered)} interrupted transaction(s).",
                file=sys.stderr,
            )

        if output.exists() and not args.force:
            raise TransactionError(
                f"output file already exists (use --force to replace it): {output}"
            )
        if output.exists() and (output.is_symlink() or not output.is_file()):
            raise TransactionError(f"output path is not a regular file: {output}")

        sources = discover_sources(root, {config_path, output})
        tasks = build_work_tasks(sources, config.encoding)
        input_bytes = sum(source.size for source in sources)

        if args.verbose:
            print(
                f"Selected {len(sources)} file(s), {input_bytes} byte(s), "
                f"{len(tasks)} task(s), {workers} worker(s).",
                file=sys.stderr,
            )

        if args.dry_run:
            print(
                f"Would process {len(sources)} file(s), {input_bytes} byte(s); "
                f"output: {output}"
            )
            return 0

        transaction = Transaction.create(root, config.output, sources)
        try:
            result = execute_tasks(
                tasks=tasks,
                fragment_directory=transaction.fragment_directory,
                workers=workers,
                rules=rules,
                encoding=config.encoding,
            )
            _ensure_merge_space(
                root,
                result.output_bytes,
                output,
            )
            merge_fragments(transaction.fragment_directory, transaction.final_path)
            verify_sources_unchanged(sources)
            transaction.commit(force=args.force)
        except BaseException:
            transaction.abort_if_working()
            raise

        print(
            f"Processed {len(sources)} file(s); input {result.input_bytes} byte(s); "
            f"output {result.output_bytes} byte(s); output: {output}"
        )
        return 0


def _ensure_merge_space(root: Path, output_bytes: int, output: Path) -> None:
    location = output.parent if output.parent.exists() else root
    free = shutil.disk_usage(location).free
    required = output_bytes + _MERGE_SPACE_MARGIN
    if free < required:
        raise FileSystemError(
            f"insufficient free space to merge output: need {required} byte(s), "
            f"have {free}"
        )


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed
