from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from ..cli import _positive_integer
from ..config import ConfigError
from ..engine import ProcessingError
from ..files import FileSystemError, LockError
from ..runner import ProgressEvent, run_processing
from ..transaction import TransactionError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="logreduce-gui-worker")
    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--workers", type=_positive_integer)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    def emit_progress(event: ProgressEvent) -> None:
        _emit({"type": "progress", **asdict(event)})

    try:
        summary = run_processing(
            log_dir=args.log_dir,
            config_path=args.config,
            workers=args.workers,
            force=args.force,
            dry_run=args.dry_run,
            progress=emit_progress,
        )
    except ConfigError as exc:
        _emit_error(exc, "configuration", 2)
        return 2
    except LockError as exc:
        _emit_error(exc, "lock", 1)
        return 1
    except (FileSystemError, ProcessingError, TransactionError) as exc:
        _emit_error(exc, "processing", 1)
        return 1
    except KeyboardInterrupt as exc:
        _emit_error(exc or "interrupted", "interrupted", 130)
        return 130
    except Exception as exc:
        _emit_error(exc, "unexpected", 1)
        return 1

    result = asdict(summary)
    result["output_path"] = str(summary.output_path)
    _emit({"type": "result", **result})
    return 0


def _emit_error(exc: BaseException, kind: str, exit_code: int) -> None:
    _emit(
        {
            "type": "error",
            "kind": kind,
            "message": str(exc) or exc.__class__.__name__,
            "exit_code": exit_code,
        }
    )


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    sys.exit(main())
