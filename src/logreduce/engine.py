from __future__ import annotations

import io
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .config import CompiledRule, compile_rules
from .files import FileRange, WorkTask


_COPY_BUFFER_BYTES = 1024 * 1024
_TEXT_BUFFER_BYTES = 1024 * 1024
_WORKER_RULES: tuple[CompiledRule, ...] = ()
_WORKER_ENCODING = "utf-8"


class ProcessingError(RuntimeError):
    """Raised when a worker cannot transform its assigned input."""


@dataclass(frozen=True, slots=True)
class TaskResult:
    index: int
    input_bytes: int
    output_bytes: int


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    input_bytes: int
    output_bytes: int


def execute_tasks(
    tasks: list[WorkTask],
    fragment_directory: Path,
    workers: int,
    rules: tuple[CompiledRule, ...],
    encoding: str,
) -> ProcessingResult:
    fragment_directory.mkdir(parents=True, exist_ok=True)
    if not tasks:
        return ProcessingResult(input_bytes=0, output_bytes=0)

    if workers <= 1 or len(tasks) == 1:
        results = [
            _process_task_direct(
                task,
                fragment_directory,
                rules,
                encoding,
            )
            for task in tasks
        ]
        return _summarize(results)

    context = multiprocessing.get_context("fork")
    max_workers = min(workers, len(tasks))
    try:
        with ProcessPoolExecutor(
            max_workers=max_workers,
            mp_context=context,
            initializer=_initialize_worker,
            initargs=(rules, encoding),
        ) as executor:
            futures = {
                executor.submit(
                    _process_task_worker,
                    task,
                    fragment_directory,
                ): task
                for task in tasks
            }
            results: list[TaskResult] = []
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    for pending in futures:
                        pending.cancel()
                    task = futures[future]
                    raise ProcessingError(
                        f"failed while processing task {task.index}: {exc}"
                    ) from exc
    except ProcessingError:
        raise
    except Exception as exc:
        raise ProcessingError(f"worker pool failed: {exc}") from exc

    results.sort(key=lambda result: result.index)
    return _summarize(results)


def available_workers() -> int:
    try:
        affinity = os.sched_getaffinity(0)
    except (AttributeError, OSError):
        affinity = ()
    return max(1, len(affinity) if affinity else (os.cpu_count() or 1))


def merge_fragments(fragment_directory: Path, output: Path) -> int:
    fragments = sorted(fragment_directory.glob("*.part"))
    total = 0
    destination_fd = os.open(
        output,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o666,
    )
    try:
        for fragment in fragments:
            source_fd = os.open(fragment, os.O_RDONLY | os.O_CLOEXEC)
            try:
                remaining = os.fstat(source_fd).st_size
                total += remaining
                _copy_fd_exact(source_fd, destination_fd, remaining)
            finally:
                os.close(source_fd)
        os.fsync(destination_fd)
    finally:
        os.close(destination_fd)
    return total


def _initialize_worker(
    rule_specs: tuple[CompiledRule, ...] | tuple,
    encoding: str,
) -> None:
    global _WORKER_RULES, _WORKER_ENCODING
    if rule_specs and isinstance(rule_specs[0], CompiledRule):
        # Fork inherits compiled patterns; this branch keeps spawn compatibility.
        _WORKER_RULES = rule_specs
    else:
        _WORKER_RULES = compile_rules(rule_specs)
    _WORKER_ENCODING = encoding


def _process_task_direct(
    task: WorkTask,
    fragment_directory: Path,
    rules: tuple[CompiledRule, ...],
    encoding: str,
) -> TaskResult:
    try:
        return _process_task(task, fragment_directory, rules, encoding)
    except Exception as exc:
        raise ProcessingError(
            f"failed while processing task {task.index}: {exc}"
        ) from exc


def _process_task_worker(
    task: WorkTask,
    fragment_directory: Path,
) -> TaskResult:
    return _process_task(
        task,
        fragment_directory,
        _WORKER_RULES,
        _WORKER_ENCODING,
    )


def _process_task(
    task: WorkTask,
    fragment_directory: Path,
    rules: tuple[CompiledRule, ...],
    encoding: str,
) -> TaskResult:
    fragment = fragment_directory / f"{task.index:012d}.part"
    if not rules:
        _copy_task_bytes(task, fragment)
    else:
        _transform_task(task, fragment, rules, encoding)
    return TaskResult(
        index=task.index,
        input_bytes=task.input_bytes,
        output_bytes=fragment.stat().st_size,
    )


def _copy_task_bytes(task: WorkTask, fragment: Path) -> None:
    with fragment.open("xb") as output:
        for item in task.ranges:
            with item.path.open("rb") as source:
                source.seek(item.offset)
                _copy_exact(source, output, item.length)


def _copy_exact(source: io.BufferedIOBase, output: io.BufferedIOBase, count: int) -> None:
    remaining = count
    while remaining:
        block = source.read(min(_COPY_BUFFER_BYTES, remaining))
        if not block:
            raise ProcessingError("source ended before its recorded size")
        output.write(block)
        remaining -= len(block)


def _copy_fd_exact(source_fd: int, destination_fd: int, count: int) -> None:
    remaining = count
    while remaining:
        try:
            copied = os.copy_file_range(
                source_fd,
                destination_fd,
                min(remaining, 1024 * 1024 * 1024),
            )
        except OSError:
            copied = 0
        if copied:
            remaining -= copied
            continue

        while remaining:
            block = os.read(source_fd, min(_COPY_BUFFER_BYTES, remaining))
            if not block:
                raise ProcessingError("fragment ended before its recorded size")
            view = memoryview(block)
            while view:
                written = os.write(destination_fd, view)
                if written == 0:
                    raise ProcessingError("could not write merged output")
                view = view[written:]
            remaining -= len(block)
        return


def _transform_task(
    task: WorkTask,
    fragment: Path,
    rules: tuple[CompiledRule, ...],
    encoding: str,
) -> None:
    raw_output = fragment.open("xb")
    text_output = io.TextIOWrapper(
        raw_output,
        encoding=encoding,
        errors="strict",
        newline="",
    )
    try:
        for item in task.ranges:
            _transform_range(item, text_output, rules, encoding)
        text_output.flush()
    finally:
        try:
            text_output.flush()
        finally:
            raw_output = text_output.detach()
            raw_output.close()


def _transform_range(
    item: FileRange,
    output: io.TextIOBase,
    rules: tuple[CompiledRule, ...],
    encoding: str,
) -> None:
    with _RangeReader(item) as raw:
        buffered = io.BufferedReader(raw, buffer_size=_TEXT_BUFFER_BYTES)
        with io.TextIOWrapper(
            buffered,
            encoding=encoding,
            errors="strict",
            newline="",
        ) as source:
            for line in source:
                transformed = _apply_rules(line, rules)
                if transformed is not None:
                    output.write(transformed)


def _apply_rules(
    line: str,
    rules: tuple[CompiledRule, ...],
) -> str | None:
    if line.endswith("\r\n"):
        content = line[:-2]
        ending = "\r\n"
    elif line.endswith(("\n", "\r")):
        content = line[:-1]
        ending = line[-1:]
    else:
        content = line
        ending = ""

    for rule in rules:
        if rule.filter_re is not None:
            if rule.filter_literal is not None:
                matched = content.find(rule.filter_literal) >= 0
            else:
                matched = rule.filter_re.search(content) is not None
            if matched != rule.include_matches:
                return None
        if rule.replace_re is not None and rule.replacement is not None:
            if (
                rule.replace_literal is not None
                and rule.replacement_literal is not None
            ):
                content = content.replace(
                    rule.replace_literal,
                    rule.replacement_literal,
                )
            else:
                content = rule.replace_re.sub(rule.replacement, content)
    return content + ending


class _RangeReader(io.RawIOBase):
    def __init__(self, item: FileRange) -> None:
        super().__init__()
        self._file = item.path.open("rb")
        self._file.seek(item.offset)
        self._remaining = item.length

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray | memoryview) -> int:
        if self._remaining <= 0:
            return 0
        amount = min(len(buffer), self._remaining)
        data = self._file.read(amount)
        if not data:
            raise ProcessingError("source ended before its recorded size")
        view = memoryview(buffer)
        view[: len(data)] = data
        self._remaining -= len(data)
        return len(data)

    def close(self) -> None:
        if not self.closed:
            self._file.close()
        super().close()


def _summarize(results: list[TaskResult]) -> ProcessingResult:
    return ProcessingResult(
        input_bytes=sum(result.input_bytes for result in results),
        output_bytes=sum(result.output_bytes for result in results),
    )
