"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import Config, load_config
from .discovery import discover
from .errors import ConfigError, DiscoveryError, LogReduceError, ProcessingError
from .models import SourceFile
from .rules import Ruleset
from .runner import RunOptions, run
from .stats import RunStats, human_bytes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="logreduce",
        description=(
            "按 TOML 规则流式筛选/替换日志目录中的文件，原地处理后将结果合并为一个文件，"
            "并在成功后删除源文件。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  logreduce -d /var/log/app -c logreduce.toml -o merged.log\n"
            "  logreduce -d ./logs -c logreduce.toml --mode direct --jobs 8\n"
            "  logreduce --validate -c logreduce.toml\n"
        ),
    )
    parser.add_argument("-c", "--config", help="TOML 配置文件路径")
    parser.add_argument("-d", "--log-dir", help="待处理的日志目录")
    parser.add_argument("-o", "--output", help="合并后的输出文件（覆盖配置 [output].path）")
    parser.add_argument(
        "--mode",
        choices=("inplace", "direct"),
        help="inplace=逐文件原地处理后合并；direct=单遍直写合并（更快，源文件在处理期间不变）",
    )
    parser.add_argument("--jobs", type=int, help="并行进程数；0 表示自动，上限 256")
    parser.add_argument("--dry-run", action="store_true", help="只展示将处理的文件，不修改任何文件")
    parser.add_argument("--keep-sources", action="store_true", help="合并成功后保留源文件")
    parser.add_argument("--force", action="store_true", help="允许覆盖已存在的输出文件")
    parser.add_argument("--no-fsync", action="store_true", help="跳过 fsync（更快，但掉电时可能丢失数据）")
    parser.add_argument(
        "--allow-changing",
        action="store_true",
        help="允许文件在处理期间被其他进程修改（默认检测到变化即中止）",
    )
    parser.add_argument("--stats", action="store_true", help="输出完整统计信息")
    parser.add_argument("--stats-json", action="store_true", help="以 JSON 输出统计信息")
    parser.add_argument("--quiet", action="store_true", help="不输出进度和摘要")
    parser.add_argument("--verbose", action="store_true", help="输出批次进度")
    parser.add_argument("--validate", action="store_true", help="仅校验配置文件，不处理文件")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.config is None:
        parser.error("缺少 -c/--config 配置文件路径")
    if args.jobs is not None and args.jobs < 0:
        parser.error("--jobs 不能小于 0")
    if not args.validate and args.log_dir is None:
        parser.error("缺少 -d/--log-dir 日志目录")

    try:
        return _run(args, parser)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0
    except (LogReduceError, OSError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130


def _run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    config_path = Path(args.config).expanduser()
    config = load_config(config_path)

    if args.validate:
        _validate(config, args)
        return 0

    assert args.log_dir is not None
    log_dir = Path(args.log_dir).expanduser()
    try:
        log_dir = log_dir.resolve()
    except OSError as exc:
        raise DiscoveryError(f"无法解析日志目录 {log_dir}: {exc}") from exc
    if not log_dir.exists():
        raise DiscoveryError(f"日志目录不存在: {log_dir}")
    if not log_dir.is_dir():
        raise DiscoveryError(f"日志路径不是目录: {log_dir}")

    output = _resolve_output(args, config, log_dir, config_path)
    mode = args.mode or config.engine.mode
    jobs = args.jobs if args.jobs is not None else config.engine.jobs
    if jobs < 0:
        raise ConfigError("jobs 不能小于 0")

    try:
        separator = config.output.separator.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ConfigError(f"[output].separator 无法编码为 UTF-8: {exc}") from exc

    fsync = config.engine.fsync and not args.no_fsync
    allow_changing = config.engine.allow_changing or args.allow_changing
    delete_sources = config.output.delete_sources and not args.keep_sources

    files = discover(
        log_dir,
        config.input,
        output,
        config.engine.sort,
        skip_paths=[config_path],
    )
    if not files:
        raise ProcessingError(
            "没有匹配的输入文件: "
            f"{log_dir} (include={list(config.input.include)}, "
            f"exclude={list(config.input.exclude)})"
        )

    if args.verbose and not args.quiet:
        print(
            f"模式={mode}，文件={len(files)}，jobs={jobs or 'auto'}，"
            f"fsync={fsync}，删除源文件={delete_sources}",
            file=sys.stderr,
            flush=True,
        )

    if args.dry_run:
        _print_dry_run(args, config, log_dir, output, files, mode)
        return 0

    options = RunOptions(
        mode=mode,
        jobs=jobs,
        chunk_size=config.engine.chunk_size,
        fsync=fsync,
        delete_sources=delete_sources,
        allow_changing=allow_changing,
        separator=separator,
        output=output,
        verbose=args.verbose and not args.quiet,
        progress=lambda message: print(message, file=sys.stderr, flush=True),
    )
    stats = run(files, config, options)
    _print_stats(args, stats)
    return 0


def _validate(config: Config, args: argparse.Namespace) -> None:
    # Compiling the rules validates every regex and replacement.
    Ruleset(config.rules)
    if args.quiet:
        return
    print(f"配置有效: {len(config.rules)} 条规则")
    print(f"默认模式: {config.engine.mode}")
    print(f"include: {', '.join(config.input.include)}")
    if config.input.exclude:
        print(f"exclude: {', '.join(config.input.exclude)}")
    if config.output.path is not None:
        print(f"配置输出: {config.output.path}")
    print(f"chunk_size: {human_bytes(config.engine.chunk_size)}")
    print(f"fsync: {config.engine.fsync}，sort: {config.engine.sort}")


def _resolve_output(
    args: argparse.Namespace,
    config: Config,
    log_dir: Path,
    config_path: Path,
) -> Path:
    raw = args.output if args.output is not None else config.output.path
    if raw is None:
        raise ConfigError(
            "未指定输出文件：请使用 -o/--output 或配置 [output].path"
        )
    raw_path = Path(raw).expanduser()
    if raw_path.is_symlink():
        raise ConfigError(f"输出路径不能是符号链接: {raw_path}")
    try:
        output = raw_path.resolve(strict=False)
    except OSError as exc:
        raise ConfigError(f"无法解析输出路径 {raw_path}: {exc}") from exc

    try:
        config_resolved = config_path.resolve()
    except OSError:
        config_resolved = config_path.absolute()
    if output == config_resolved:
        raise ConfigError("输出路径不能与配置文件相同")
    if output == log_dir:
        raise ConfigError("输出路径不能是日志目录本身")

    if output.exists():
        try:
            if os.path.samefile(output, config_path):
                raise ConfigError("输出路径不能与配置文件是同一个文件（硬链接）")
        except FileNotFoundError:
            pass
        except OSError:
            pass
        if output.is_dir():
            raise ConfigError(f"输出路径是目录: {output}")
        if not output.is_file():
            raise ConfigError(f"输出路径不是普通文件: {output}")
        if not args.dry_run and not args.force:
            raise ProcessingError(
                f"输出文件已存在: {output}（使用 --force 覆盖）"
            )

    parent = output.parent
    if parent.exists() and not parent.is_dir():
        raise ConfigError(f"输出目录不是目录: {parent}")
    return output


def _print_dry_run(
    args: argparse.Namespace,
    config: Config,
    log_dir: Path,
    output: Path,
    files: Sequence[SourceFile],
    mode: str,
) -> None:
    total = sum(item.size for item in files)
    if args.stats_json:
        payload = {
            "dry_run": True,
            "mode": mode,
            "log_dir": str(log_dir),
            "output": str(output),
            "output_exists": output.exists(),
            "rules": len(config.rules),
            "files": [
                {"path": item.rel, "bytes": item.size} for item in files
            ],
            "total_bytes": total,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if args.quiet:
        return

    print(f"模式: {mode}")
    print(f"日志目录: {log_dir}")
    print(f"输出文件: {output}" + ("（已存在，将被覆盖）" if output.exists() else ""))
    print(f"规则: {len(config.rules)} 条")
    print(
        "删除源文件: "
        + ("true" if config.output.delete_sources and not args.keep_sources else "false")
    )
    print(f"输入文件: {len(files)} 个，共 {human_bytes(total)}")
    limit = 20
    for index, item in enumerate(files[:limit], 1):
        print(f"  {index:>3}. {item.rel} ({human_bytes(item.size)})")
    if len(files) > limit:
        print(f"  ... 还有 {len(files) - limit} 个文件")
    print("dry-run: 未修改任何文件")


def _print_stats(args: argparse.Namespace, stats: RunStats) -> None:
    if args.stats_json:
        print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))
    elif args.stats:
        print(stats.format_human())
    elif not args.quiet:
        print(stats.format_short())


if __name__ == "__main__":
    raise SystemExit(main())
