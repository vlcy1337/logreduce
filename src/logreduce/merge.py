"""Merging processed shards/files and removing sources safely."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .errors import MergeError
from .fsutil import copy_fd, default_file_mode, fsync_dir, write_all
from .models import SourceFile


def read_last_byte(path: Path) -> int | None:
    size = path.stat().st_size
    if size == 0:
        return None
    fd = os.open(path, os.O_RDONLY)
    try:
        data = os.pread(fd, 1, size - 1)
    finally:
        os.close(fd)
    return data[0] if data else None


def merge_paths(
    paths: Sequence[Path],
    last_bytes: Sequence[int | None],
    output: Path,
    separator: bytes,
    fsync: bool,
    group_ids: Sequence[int] | None = None,
) -> None:
    """Concatenate ``paths`` into ``output`` atomically.

    ``last_bytes`` may contain ``None`` for empty files.  A separator is
    inserted only before a non-empty source when the previous non-empty source
    did not end with LF.  Paths with the same ``group_id`` belong to the same
    original file (parallel range chunks) and are concatenated without a
    separator.
    """

    if len(paths) != len(last_bytes):
        raise MergeError("内部错误: paths/last_bytes 长度不一致")
    if group_ids is None:
        groups: Sequence[int] = range(len(paths))
    else:
        if len(group_ids) != len(paths):
            raise MergeError("内部错误: paths/group_ids 长度不一致")
        groups = group_ids

    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".logreduce-out-", dir=str(output.parent))
    tmp = Path(tmp_name)
    previous_last: int | None = None
    previous_group: int | None = None
    try:
        os.chmod(tmp, default_file_mode())
        for path, last_byte, group_id in zip(paths, last_bytes, groups):
            try:
                size = path.stat().st_size
            except OSError as exc:
                raise MergeError(f"无法读取待合并文件 {path}: {exc}") from exc
            if size == 0:
                continue
            if (
                previous_group is not None
                and group_id != previous_group
                and previous_last is not None
                and previous_last != 10
                and separator
            ):
                write_all(fd, separator)
            src_fd = os.open(path, os.O_RDONLY)
            try:
                copy_fd(src_fd, fd)
            except OSError as exc:
                raise MergeError(f"合并文件 {path} 失败: {exc}") from exc
            finally:
                os.close(src_fd)
            previous_last = last_byte if last_byte is not None else read_last_byte(path)
            previous_group = group_id

        if fsync:
            os.fsync(fd)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)

    try:
        os.replace(tmp, output)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if fsync:
        fsync_dir(output.parent)


def delete_sources(sources: Sequence[SourceFile], fsync: bool) -> list[str]:
    """Unlink every source path (including hard-link aliases).

    Returns human-readable errors instead of raising immediately so that one
    stubborn file does not prevent the others from being removed.
    """

    errors: list[str] = []
    directories: set[Path] = set()
    for source in sources:
        for path in source.all_paths:
            directories.add(path.parent)
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                errors.append(f"{path}: {exc}")

    if fsync:
        for directory in directories:
            try:
                fsync_dir(directory)
            except OSError as exc:
                errors.append(f"{directory}: fsync 失败: {exc}")
    return errors
