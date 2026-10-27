from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .config import compile_rules, load_config
from .engine import available_workers, execute_tasks, merge_fragments
from .files import (
    DirectoryLock,
    FileSystemError,
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


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    phase: str
    message: str
    completed_tasks: int = 0
    total_tasks: int = 0
    input_bytes: int = 0
    output_bytes: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RunSummary:
    file_count: int
    input_bytes: int
    output_bytes: int
    output_path: Path
    workers: int
    task_count: int
    dry_run: bool
    recovered_transactions: int


ProgressCallback = Callable[[ProgressEvent], None]


def run_processing(
    *,
    log_dir: str,
    config_path: str,
    workers: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    progress: ProgressCallback | None = None,
) -> RunSummary:
    """Run one complete LogReduce operation using the shared safe workflow."""

    _emit(progress, "validating", "正在校验目录和配置。")
    root = resolve_log_directory(log_dir)
    resolved_config_path = resolve_config_path(config_path)
    config = load_config(resolved_config_path)
    output = resolve_output_path(root, config.output)
    if output == resolved_config_path:
        raise FileSystemError("output path must not overwrite the configuration file")

    worker_count = workers or available_workers()
    if worker_count < 1:
        raise ValueError("worker count must be at least 1")
    rules = compile_rules(config.rules)

    _emit(progress, "waiting", "正在获取日志目录锁。")
    with DirectoryLock(root):
        if dry_run:
            pending = pending_transaction_directories(root)
            if pending:
                raise TransactionError(
                    "an interrupted transaction requires recovery; run without "
                    "--dry-run first"
                )
        recovered = recover_pending_transactions(root)
        if recovered:
            _emit(
                progress,
                "recovered",
                f"已恢复 {len(recovered)} 个中断事务。",
            )

        if output.exists() and not force:
            raise TransactionError(
                f"output file already exists (use --force to replace it): {output}"
            )
        if output.exists() and (output.is_symlink() or not output.is_file()):
            raise TransactionError(f"output path is not a regular file: {output}")

        sources = discover_sources(root, {resolved_config_path, output})
        tasks = build_work_tasks(sources, config.encoding)
        input_bytes = sum(source.size for source in sources)
        _emit(
            progress,
            "selected",
            (
                f"已选择 {len(sources)} 个文件，共 {input_bytes} 字节；"
                f"{len(tasks)} 个任务，{worker_count} 个 worker。"
            ),
            completed_tasks=0,
            total_tasks=len(tasks),
            input_bytes=0,
            output_bytes=0,
        )

        if dry_run:
            _emit(
                progress,
                "dry_run",
                f"预检完成；输出路径为 {output}。",
                total_tasks=len(tasks),
            )
            return RunSummary(
                file_count=len(sources),
                input_bytes=input_bytes,
                output_bytes=0,
                output_path=output,
                workers=worker_count,
                task_count=len(tasks),
                dry_run=True,
                recovered_transactions=len(recovered),
            )

        transaction = Transaction.create(root, config.output, sources)
        try:
            _emit(
                progress,
                "processing",
                "正在筛选和替换日志。",
                total_tasks=len(tasks),
            )
            result = execute_tasks(
                tasks=tasks,
                fragment_directory=transaction.fragment_directory,
                workers=worker_count,
                rules=rules,
                encoding=config.encoding,
                task_progress=lambda completed, total, input_count, output_count: _emit(
                    progress,
                    "processing",
                    f"已处理 {completed}/{total} 个任务。",
                    completed_tasks=completed,
                    total_tasks=total,
                    input_bytes=input_count,
                    output_bytes=output_count,
                ),
            )
            _emit(
                progress,
                "merging",
                "正在合并输出片段。",
                completed_tasks=len(tasks),
                total_tasks=len(tasks),
                input_bytes=result.input_bytes,
                output_bytes=result.output_bytes,
            )
            _ensure_merge_space(root, result.output_bytes, output)
            merge_fragments(transaction.fragment_directory, transaction.final_path)
            _emit(
                progress,
                "committing",
                "正在校验源文件并提交事务。",
                completed_tasks=len(tasks),
                total_tasks=len(tasks),
                input_bytes=result.input_bytes,
                output_bytes=result.output_bytes,
            )
            verify_sources_unchanged(sources)
            transaction.commit(force=force)
        except BaseException:
            transaction.abort_if_working()
            raise

    summary = RunSummary(
        file_count=len(sources),
        input_bytes=result.input_bytes,
        output_bytes=result.output_bytes,
        output_path=output,
        workers=worker_count,
        task_count=len(tasks),
        dry_run=False,
        recovered_transactions=len(recovered),
    )
    _emit(
        progress,
        "complete",
        f"处理完成；输出文件为 {output}。",
        completed_tasks=len(tasks),
        total_tasks=len(tasks),
        input_bytes=summary.input_bytes,
        output_bytes=summary.output_bytes,
    )
    return summary


def _ensure_merge_space(root: Path, output_bytes: int, output: Path) -> None:
    location = output.parent if output.parent.exists() else root
    free = shutil.disk_usage(location).free
    required = output_bytes + _MERGE_SPACE_MARGIN
    if free < required:
        raise FileSystemError(
            f"insufficient free space to merge output: need {required} byte(s), "
            f"have {free}"
        )


def _emit(
    callback: ProgressCallback | None,
    phase: str,
    message: str,
    *,
    completed_tasks: int = 0,
    total_tasks: int = 0,
    input_bytes: int = 0,
    output_bytes: int = 0,
) -> None:
    if callback is not None:
        callback(
            ProgressEvent(
                phase=phase,
                message=message,
                completed_tasks=completed_tasks,
                total_tasks=total_tasks,
                input_bytes=input_bytes,
                output_bytes=output_bytes,
            )
        )
