from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


TASK_TARGET_BYTES = 8 * 1024 * 1024
_BOUNDARY_SCAN_BYTES = 64 * 1024 * 1024
_SCAN_STEP_BYTES = 1024 * 1024


class FileSystemError(RuntimeError):
    """Raised when source files cannot be safely processed."""


class LockError(FileSystemError):
    """Raised when another LogReduce process owns the log directory."""


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: Path
    relative_path: str
    size: int
    mtime_ns: int
    inode: int
    device: int


@dataclass(frozen=True, slots=True)
class FileRange:
    path: Path
    relative_path: str
    offset: int
    length: int


@dataclass(frozen=True, slots=True)
class WorkTask:
    index: int
    ranges: tuple[FileRange, ...]
    input_bytes: int


class DirectoryLock:
    """Advisory exclusive lock held on the log directory itself."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._fd: int | None = None

    def __enter__(self) -> DirectoryLock:
        fd = os.open(self._directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise LockError(
                f"another LogReduce process is using {self._directory}"
            ) from exc
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, *_args: object) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


def resolve_log_directory(value: str) -> Path:
    path = Path(value).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise FileSystemError(f"cannot resolve log directory {path}: {exc}") from exc
    if not resolved.is_dir():
        raise FileSystemError(f"log directory is not a directory: {resolved}")
    return resolved


def resolve_config_path(value: str) -> Path:
    path = Path(value).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise FileSystemError(f"cannot resolve configuration {path}: {exc}") from exc
    if not resolved.is_file():
        raise FileSystemError(f"configuration is not a file: {resolved}")
    return resolved


def resolve_output_path(root: Path, relative_output: str) -> Path:
    parts = PurePosixPath(relative_output).parts
    lexical = root.joinpath(*parts)
    resolved_parent = lexical.parent.resolve(strict=False)
    if not resolved_parent.is_relative_to(root):
        raise FileSystemError("output path escapes the log directory")
    output = resolved_parent / lexical.name
    if output == root:
        raise FileSystemError("output path must name a file")
    return output


def discover_sources(root: Path, excluded_paths: set[Path]) -> list[SourceFile]:
    excluded = {_normalized(path) for path in excluded_paths}
    sources: list[SourceFile] = []

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise FileSystemError(f"cannot scan {directory}: {exc}") from exc

        for entry in entries:
            path = Path(entry.path)
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if directory == root and entry.name.startswith(
                        ".logreduce-txn-"
                    ):
                        continue
                    visit(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                normalized = _normalized(path)
                if normalized in excluded:
                    continue
                stat_result = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise FileSystemError(f"cannot inspect {path}: {exc}") from exc

            sources.append(
                SourceFile(
                    path=normalized,
                    relative_path=path.relative_to(root).as_posix(),
                    size=stat_result.st_size,
                    mtime_ns=stat_result.st_mtime_ns,
                    inode=stat_result.st_ino,
                    device=stat_result.st_dev,
                )
            )

    visit(root)
    sources.sort(key=lambda source: source.relative_path)
    return sources


def build_work_tasks(
    sources: list[SourceFile],
    encoding: str,
) -> list[WorkTask]:
    chunkable = _encoding_is_chunkable(encoding)
    tasks: list[tuple[FileRange, ...]] = []
    batch: list[FileRange] = []
    batch_bytes = 0

    def flush_batch() -> None:
        nonlocal batch, batch_bytes
        if batch:
            tasks.append(tuple(batch))
            batch = []
            batch_bytes = 0

    for source in sources:
        should_split = chunkable and source.size > TASK_TARGET_BYTES
        should_isolate = not chunkable
        if should_split:
            flush_batch()
            for start, length in _split_ranges(source):
                tasks.append(
                    (
                        FileRange(
                            path=source.path,
                            relative_path=source.relative_path,
                            offset=start,
                            length=length,
                        ),
                    )
                )
            continue

        if should_isolate:
            flush_batch()
            tasks.append(
                (
                    FileRange(
                        path=source.path,
                        relative_path=source.relative_path,
                        offset=0,
                        length=source.size,
                    ),
                )
            )
            continue

        if batch and batch_bytes + source.size > TASK_TARGET_BYTES:
            flush_batch()
        batch.append(
            FileRange(
                path=source.path,
                relative_path=source.relative_path,
                offset=0,
                length=source.size,
            )
        )
        batch_bytes += source.size

    flush_batch()
    return [
        WorkTask(
            index=index,
            ranges=ranges,
            input_bytes=sum(item.length for item in ranges),
        )
        for index, ranges in enumerate(tasks)
    ]


def verify_sources_unchanged(sources: list[SourceFile]) -> None:
    for source in sources:
        try:
            stat_result = source.path.lstat()
        except OSError as exc:
            raise FileSystemError(
                f"source file disappeared or became inaccessible: "
                f"{source.relative_path}: {exc}"
            ) from exc
        actual = (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            stat_result.st_mtime_ns,
        )
        expected = (
            source.device,
            source.inode,
            source.size,
            source.mtime_ns,
        )
        if actual != expected:
            raise FileSystemError(
                f"source file changed during processing: {source.relative_path}"
            )


def _split_ranges(source: SourceFile) -> list[tuple[int, int]]:
    if source.size == 0:
        return [(0, 0)]

    ranges: list[tuple[int, int]] = []
    start = 0
    fd = os.open(source.path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        while start < source.size:
            desired_end = min(start + TASK_TARGET_BYTES, source.size)
            if desired_end == source.size:
                end = source.size
            else:
                newline = _find_newline(fd, desired_end, source.size)
                end = source.size if newline is None else newline + 1
            ranges.append((start, end - start))
            start = end
    finally:
        os.close(fd)
    return ranges


def _find_newline(fd: int, start: int, size: int) -> int | None:
    limit = min(size, start + _BOUNDARY_SCAN_BYTES)
    position = start
    while position < limit:
        block = os.pread(fd, min(_SCAN_STEP_BYTES, limit - position), position)
        if not block:
            return None
        index = block.find(b"\n")
        if index >= 0:
            return position + index
        position += len(block)
    return None


def _encoding_is_chunkable(encoding: str) -> bool:
    try:
        return "\n".encode(encoding) == b"\n"
    except (LookupError, UnicodeError):
        return False


def _normalized(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError as exc:
        raise FileSystemError(f"cannot resolve {path}: {exc}") from exc
