"""Run orchestration: batching, parallelism, merge and deletion."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .engine import (
    process_batch,
    process_inplace_batch,
    process_range,
    worker_direct,
    worker_init,
    worker_inplace_batch,
)
from .errors import MergeError, ProcessingError
from .models import DirectTask, SourceFile
from .rules import Ruleset
from .stats import FileStats, RunStats

CHUNK_PARALLEL_THRESHOLD = 8 * 1024 * 1024


@dataclass
class RunOptions:
    mode: str
    jobs: int
    chunk_size: int
    fsync: bool
    delete_sources: bool
    allow_changing: bool
    separator: bytes
    output: Path
    verbose: bool = False
    progress: Callable[[str], None] | None = None


def build_batches(files: Sequence[SourceFile], jobs: int) -> list[list[SourceFile]]:
    """Split a sorted file list into contiguous byte-balanced batches."""

    if len(files) <= 1 or jobs <= 1:
        return [list(files)]
    jobs = min(jobs, len(files))
    total = sum(item.size for item in files)
    target = max(total / jobs, 1.0)

    batches: list[list[SourceFile]] = []
    current: list[SourceFile] = []
    current_size = 0
    remaining_batches = jobs
    for index, item in enumerate(files):
        current.append(item)
        current_size += item.size
        files_left = len(files) - index - 1
        if (
            remaining_batches > 1
            and current_size >= target
            and files_left >= remaining_batches - 1
        ):
            batches.append(current)
            current = []
            current_size = 0
            remaining_batches -= 1
    if current:
        batches.append(current)
    return batches


def run(files: Sequence[SourceFile], config: Config, options: RunOptions) -> RunStats:
    if not files:
        raise ProcessingError("没有可处理的文件")

    output = options.output
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProcessingError(f"无法创建输出目录 {output.parent}: {exc}") from exc
    if not output.parent.is_dir():
        raise ProcessingError(f"输出目录不是目录: {output.parent}")

    ruleset = Ruleset(config.rules)
    requested_jobs = options.jobs
    if requested_jobs <= 0:
        jobs = os.cpu_count() or 1
        # Process-pool startup is not worth it for tiny workloads.
        if sum(item.size for item in files) < (16 << 20):
            jobs = 1
    else:
        jobs = requested_jobs
    jobs = max(1, min(jobs, 256))
    options.jobs = jobs
    batches = build_batches(files, jobs)

    stats = RunStats(mode=options.mode, output=str(output))
    started = time.perf_counter()

    if options.mode == "inplace":
        _run_inplace(files, batches, ruleset, config, options, stats)
    elif options.mode == "direct":
        _run_direct(files, ruleset, config, options, stats)
    else:
        raise ProcessingError(f"未知处理模式: {options.mode}")

    stats.duration = time.perf_counter() - started
    return stats


def _run_inplace(
    files: Sequence[SourceFile],
    batches: Sequence[Sequence[SourceFile]],
    ruleset: Ruleset,
    config: Config,
    options: RunOptions,
    stats: RunStats,
) -> None:
    all_stats: list[FileStats] = []

    if len(batches) <= 1 or options.jobs == 1:
        for batch in batches:
            batch_stats = process_inplace_batch(
                batch,
                ruleset,
                options.chunk_size,
                options.fsync,
                options.allow_changing,
            )
            all_stats.extend(batch_stats)
            _emit(options, f"已完成 {len(all_stats)}/{len(files)} 个文件")
    else:
        tasks = [(batch, options.allow_changing) for batch in batches]
        results = _map_parallel(
            worker_inplace_batch,
            tasks,
            config,
            options,
            total=len(batches),
            label="文件批次",
        )
        for batch_stats in results:
            all_stats.extend(batch_stats)

    if len(all_stats) != len(files):
        raise ProcessingError(
            f"内部错误: 统计数量 {len(all_stats)} 与文件数量 {len(files)} 不一致"
        )

    stats.extend(all_stats)
    signatures: dict[Path, tuple[int, int]] | None = None
    if options.delete_sources and not options.allow_changing:
        signatures = {}
        for source in files:
            for path in source.all_paths:
                current = os.stat(path)
                signatures[path] = (current.st_size, current.st_mtime_ns)
    stats.output_bytes = _merge_and_delete(
        [item.path for item in files],
        [item.last_byte for item in all_stats],
        options,
        sources=files,
        signatures=signatures,
    )


def _run_direct(
    files: Sequence[SourceFile],
    ruleset: Ruleset,
    config: Config,
    options: RunOptions,
    stats: RunStats,
) -> None:
    temp_dir = Path(
        tempfile.mkdtemp(prefix=".logreduce-", dir=str(options.output.parent))
    )
    try:
        tasks = _build_direct_tasks(files, options.jobs, temp_dir)
        results: list[tuple[list[FileStats] | FileStats, int | None]] = []

        if len(tasks) <= 1 or options.jobs == 1:
            for index, task in enumerate(tasks, 1):
                results.append(_execute_direct_task(task, ruleset, options))
                _emit(options, f"已完成 {index}/{len(tasks)} 个分片")
        else:
            task_args = [
                (task, options.separator, options.allow_changing)
                for task in tasks
            ]
            results = _map_parallel(
                worker_direct,
                task_args,
                config,
                options,
                total=len(tasks),
                label="分片",
            )

        all_stats = _aggregate_direct_stats(files, tasks, results)
        if len(all_stats) != len(files):
            raise ProcessingError(
                f"内部错误: 统计数量 {len(all_stats)} 与文件数量 {len(files)} 不一致"
            )

        stats.extend(all_stats)
        shard_paths = [task.shard for task in tasks]
        last_bytes = [last_byte for _, last_byte in results]
        group_ids = [task.group_id for task in tasks]
        stats.output_bytes = _merge_and_delete(
            shard_paths,
            last_bytes,
            options,
            sources=files,
            group_ids=group_ids,
            verify_sources=True,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _build_direct_tasks(
    files: Sequence[SourceFile], jobs: int, temp_dir: Path
) -> list[DirectTask]:
    """Build batch tasks, or per-file/range tasks when there are spare workers.

    When ``len(files) <= jobs`` every file starts as one range task.  Large
    ranges are repeatedly split at newline boundaries until either the worker
    budget is exhausted or no range is large/splittable enough.  This avoids
    the single-huge-file bottleneck while keeping output order exact.
    """

    if len(files) > jobs:
        batches = build_batches(files, jobs)
        return [
            DirectTask(
                kind="batch",
                group_id=index,
                shard=temp_dir / f"batch-{index:06d}.part",
                batch=batch,
            )
            for index, batch in enumerate(batches)
        ]

    tasks: list[DirectTask] = [
        DirectTask(
            kind="range",
            group_id=index,
            shard=temp_dir / f"range-{index:06d}.part",
            source=source,
            start=0,
            end=source.size,
        )
        for index, source in enumerate(files)
    ]
    remaining = jobs - len(tasks)
    shard_index = len(tasks)

    while remaining > 0:
        candidates = [
            task
            for task in tasks
            if task.kind == "range"
            and task.can_split
            and task.source is not None
            and task.end - task.start >= CHUNK_PARALLEL_THRESHOLD
        ]
        if not candidates:
            break
        candidates.sort(key=lambda task: task.end - task.start, reverse=True)

        split_done = False
        for task in candidates:
            assert task.source is not None
            boundary = _find_split_offset(task.source.path, task.start, task.end)
            if boundary is None:
                task.can_split = False
                continue

            position = tasks.index(task)
            first = DirectTask(
                kind="range",
                group_id=task.group_id,
                shard=temp_dir / f"range-{shard_index:06d}.part",
                source=task.source,
                start=task.start,
                end=boundary,
            )
            shard_index += 1
            second = DirectTask(
                kind="range",
                group_id=task.group_id,
                shard=temp_dir / f"range-{shard_index:06d}.part",
                source=task.source,
                start=boundary,
                end=task.end,
            )
            shard_index += 1
            tasks[position : position + 1] = [first, second]
            remaining -= 1
            split_done = True
            break

        if not split_done:
            break

    return tasks


def _find_split_offset(path: Path, start: int, end: int) -> int | None:
    """Find the first LF after the midpoint, returning the next line start."""

    midpoint = start + (end - start) // 2
    if midpoint <= start or midpoint >= end:
        return None

    with open(path, "rb") as handle:
        handle.seek(midpoint)
        position = midpoint
        while position < end:
            data = handle.read(min(1 << 20, end - position))
            if not data:
                break
            newline = data.find(b"\n")
            if newline >= 0:
                boundary = position + newline + 1
                if start < boundary < end:
                    return boundary
                return None
            position += len(data)
    return None


def _execute_direct_task(
    task: DirectTask, ruleset: Ruleset, options: RunOptions
) -> tuple[list[FileStats] | FileStats, int | None]:
    if task.kind == "batch":
        assert task.batch is not None
        return process_batch(
            task.batch,
            task.shard,
            options.separator,
            ruleset,
            options.chunk_size,
            options.fsync,
            options.allow_changing,
        )

    assert task.source is not None
    stats = process_range(
        task.source,
        task.shard,
        task.start,
        task.end,
        ruleset,
        options.chunk_size,
        options.fsync,
        options.allow_changing,
    )
    return stats, (stats.last_byte if stats.bytes_out > 0 else None)


def _aggregate_direct_stats(
    files: Sequence[SourceFile],
    tasks: Sequence[DirectTask],
    results: Sequence[tuple[list[FileStats] | FileStats, int | None]],
) -> list[FileStats]:
    aggregated: dict[Path, FileStats] = {
        source.path: FileStats(source=str(source.path)) for source in files
    }

    for task, (result_stats, _) in zip(tasks, results):
        if task.kind == "batch":
            assert isinstance(result_stats, list)
            assert task.batch is not None
            if len(result_stats) != len(task.batch):
                raise ProcessingError("内部错误: 批次统计数量不一致")
            for source, file_stats in zip(task.batch, result_stats):
                aggregated[source.path] = file_stats
            continue

        assert isinstance(result_stats, FileStats)
        assert task.source is not None
        target = aggregated[task.source.path]
        target.bytes_in += result_stats.bytes_in
        target.bytes_out += result_stats.bytes_out
        target.lines_in += result_stats.lines_in
        target.lines_out += result_stats.lines_out
        target.lines_dropped += result_stats.lines_dropped
        target.elapsed += result_stats.elapsed
        if result_stats.bytes_out > 0:
            target.last_byte = result_stats.last_byte

    return [aggregated[source.path] for source in files]


def _merge_and_delete(
    paths: Sequence[Path],
    last_bytes: Sequence[int | None],
    options: RunOptions,
    *,
    sources: Sequence[SourceFile] | None = None,
    group_ids: Sequence[int] | None = None,
    verify_sources: bool = False,
    signatures: dict[Path, tuple[int, int]] | None = None,
) -> int:
    from .merge import delete_sources, merge_paths

    merge_paths(
        paths,
        last_bytes,
        options.output,
        options.separator,
        options.fsync,
        group_ids=group_ids,
    )
    _emit(options, f"已生成输出文件 {options.output}")
    output_size = options.output.stat().st_size

    if options.delete_sources and sources is not None:
        if (verify_sources or signatures is not None) and not options.allow_changing:
            changed: list[str] = []
            for source in sources:
                for path in source.all_paths:
                    if signatures is not None:
                        expected = signatures.get(path)
                        if expected is None:
                            continue
                    else:
                        expected = (source.size, source.mtime_ns)
                    try:
                        current = os.stat(path)
                    except FileNotFoundError:
                        continue
                    if (
                        current.st_size != expected[0]
                        or current.st_mtime_ns != expected[1]
                    ):
                        changed.append(str(path))
            if changed:
                raise MergeError(
                    "输出文件已生成，但以下源文件在运行期间发生变化，未删除:\n"
                    + "\n".join(changed)
                )

        errors = delete_sources(sources, options.fsync)
        if errors:
            raise MergeError(
                "输出文件已生成，但部分源文件删除失败:\n" + "\n".join(errors)
            )
        _emit(options, f"已删除 {len(sources)} 个源文件")
    return output_size


def _map_parallel(
    func: Any,
    tasks: Sequence[Any],
    config: Config,
    options: RunOptions,
    *,
    total: int,
    label: str,
) -> list[Any]:
    results: list[Any] = [None] * len(tasks)
    executor = ProcessPoolExecutor(
        max_workers=max(1, options.jobs),
        initializer=worker_init,
        initargs=(config.rules, options.chunk_size, options.fsync),
    )
    try:
        future_to_index = {
            executor.submit(func, task): index for index, task in enumerate(tasks)
        }
    except BaseException:
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    try:
        completed = 0
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            results[index] = future.result()
            completed += 1
            _emit(options, f"已完成 {completed}/{total} 个{label}")
    except BaseException:
        for future in future_to_index:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
        return results


def _emit(options: RunOptions, message: str) -> None:
    if options.verbose and options.progress is not None:
        options.progress(message)
