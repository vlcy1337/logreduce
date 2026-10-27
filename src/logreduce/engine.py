"""Streaming processing engine and multiprocessing workers."""

from __future__ import annotations

import errno
import os
import stat
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

from .config import DEFAULT_CHUNK_SIZE, RuleConfig
from .errors import ProcessingError
from .fsutil import fsync_dir
from .models import DirectTask, SourceFile
from .rules import BulkOp, BulkOps, Ruleset
from .stats import FileStats

_LF = b"\n"
_CRLF = b"\r\n"
_FLUSH_AT = 1 << 20


def _write_all_file(file: BinaryIO, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = file.write(view)
        if written is None:
            return
        if written <= 0:
            raise OSError(errno.EIO, "write returned 0 bytes")
        view = view[written:]


class _Output:
    """Buffer output writes and remember the final byte."""

    __slots__ = ("file", "buf", "size", "bytes_out", "last_byte", "flush_at")

    def __init__(self, file: BinaryIO, flush_at: int = _FLUSH_AT) -> None:
        self.file = file
        self.buf: list[bytes] = []
        self.size = 0
        self.bytes_out = 0
        self.last_byte: int | None = None
        self.flush_at = flush_at

    def write(self, data: bytes) -> None:
        if not data:
            return
        self.bytes_out += len(data)
        self.last_byte = data[-1]
        if not self.buf and len(data) >= self.flush_at:
            # Bulk-mode chunks: avoid the extra join copy.
            _write_all_file(self.file, data)
            return
        self.buf.append(data)
        self.size += len(data)
        if self.size >= self.flush_at:
            self.flush()

    def flush(self) -> None:
        if self.buf:
            _write_all_file(self.file, b"".join(self.buf))
            self.buf.clear()
            self.size = 0


class _LineProcessor:
    """Strict line-mode processor; feeds complete lines from arbitrary chunks."""

    __slots__ = ("process_line", "out", "carry", "lines_in", "lines_out", "lines_dropped")

    def __init__(self, ruleset: Ruleset, out: _Output) -> None:
        self.process_line = ruleset.process_line
        self.out = out
        self.carry = b""
        self.lines_in = 0
        self.lines_out = 0
        self.lines_dropped = 0

    def feed(self, data: bytes) -> None:
        if not data:
            return
        if self.carry:
            data = self.carry + data
            self.carry = b""
        parts = data.split(_LF)
        self.carry = parts.pop()
        for raw in parts:
            self._complete_line(raw)

    def finish(self) -> None:
        if self.carry:
            raw = self.carry
            self.carry = b""
            self.lines_in += 1
            result = self.process_line(raw)
            if result is None:
                self.lines_dropped += 1
                return
            self.lines_out += 1
            self.out.write(raw if result is raw else result)

    def _complete_line(self, raw: bytes) -> None:
        self.lines_in += 1
        if raw.endswith(b"\r"):
            body = raw[:-1]
            ending = _CRLF
        else:
            body = raw
            ending = _LF
        result = self.process_line(body)
        if result is None:
            self.lines_dropped += 1
            return
        self.lines_out += 1
        if result is body:
            self.out.write(raw)
            self.out.write(_LF)
        else:
            self.out.write(result)
            self.out.write(ending)


class _LimitedReader:
    """Expose only ``remaining`` bytes of an underlying binary reader."""

    __slots__ = ("file", "remaining")

    def __init__(self, file: BinaryIO, remaining: int) -> None:
        self.file = file
        self.remaining = max(0, remaining)

    def read(self, size: int) -> bytes:
        if self.remaining <= 0:
            return b""
        data = self.file.read(min(size, self.remaining))
        self.remaining -= len(data)
        return data


def process_file(
    src: Path,
    out: BinaryIO,
    ruleset: Ruleset,
    chunk_size: int,
    *,
    start: int = 0,
    end: int | None = None,
    expected: tuple[int, int] | None = None,
    check_change: bool = True,
) -> FileStats:
    """Process ``src`` into ``out``.

    ``out`` is a binary file object.  The caller owns it, flushes/fsyncs it and
    handles temporary-file lifetime.  ``expected`` is ``(size, mtime_ns)``
    captured during discovery; a mismatch after reading aborts before the
    source is replaced.
    """

    stats = FileStats(source=str(src))
    started = time.perf_counter()

    before = expected
    if check_change and before is None:
        st = os.stat(src)
        before = (st.st_size, st.st_mtime_ns)

    sink = _Output(out)
    with open(src, "rb") as fh:
        if start:
            fh.seek(start)
        reader: BinaryIO = fh if end is None else _LimitedReader(fh, end - start)  # type: ignore[assignment]
        if ruleset.bulk_ops is not None:
            _process_bulk(reader, sink, ruleset, chunk_size, stats, ruleset.bulk_ops)
        else:
            _process_lines(reader, sink, ruleset, chunk_size, stats)

    sink.flush()
    stats.bytes_out = sink.bytes_out
    stats.last_byte = sink.last_byte

    if check_change:
        after = os.stat(src)
        if before is not None and (
            after.st_size != before[0] or after.st_mtime_ns != before[1]
        ):
            raise ProcessingError(f"{src}: 文件在处理期间被修改，已中止")

    stats.elapsed = time.perf_counter() - started
    return stats


def _process_lines(
    src: BinaryIO,
    sink: _Output,
    ruleset: Ruleset,
    chunk_size: int,
    stats: FileStats,
) -> None:
    processor = _LineProcessor(ruleset, sink)
    bytes_in = 0
    while True:
        chunk = src.read(chunk_size)
        if not chunk:
            break
        bytes_in += len(chunk)
        processor.feed(chunk)
    processor.finish()

    stats.bytes_in = bytes_in
    stats.lines_in = processor.lines_in
    stats.lines_out = processor.lines_out
    stats.lines_dropped = processor.lines_dropped


def _process_bulk(
    src: BinaryIO,
    sink: _Output,
    ruleset: Ruleset,
    chunk_size: int,
    stats: FileStats,
    bulk: BulkOps,
) -> None:
    ops = bulk.ops
    has_regex = bulk.has_regex
    carry = b""
    bytes_in = 0
    lines_in = 0
    lines_out = 0
    line_processor: _LineProcessor | None = None

    while True:
        chunk = src.read(chunk_size)
        if not chunk:
            break
        bytes_in += len(chunk)

        if line_processor is not None:
            line_processor.feed(chunk)
            continue

        if carry:
            chunk = carry + chunk
            carry = b""

        newline = chunk.rfind(_LF)
        if newline < 0:
            carry = chunk
            continue

        complete = chunk[: newline + 1]
        carry = chunk[newline + 1 :]

        if has_regex and b"\r\n" in complete:
            # CRLF changes how '.' and '$' see line endings.  Switch to strict
            # line mode for the rest of this file.
            line_processor = _LineProcessor(ruleset, sink)
            line_processor.feed(complete)
            line_processor.carry = carry
            carry = b""
            continue

        data = _apply_bulk(ops, complete)
        sink.write(data)
        lines_in += complete.count(_LF)
        lines_out += data.count(_LF)

    if line_processor is not None:
        line_processor.finish()
        lines_in += line_processor.lines_in
        lines_out += line_processor.lines_out
        dropped = line_processor.lines_dropped
    else:
        if carry:
            data = _apply_bulk(ops, carry)
            sink.write(data)
            lines_in += 1
            lines_out += 1
        dropped = 0

    stats.bytes_in = bytes_in
    stats.lines_in = lines_in
    stats.lines_out = lines_out
    stats.lines_dropped = dropped


def _apply_bulk(ops: Sequence[BulkOp], data: bytes) -> bytes:
    for op in ops:
        if op.kind == "literal":
            data = data.replace(op.pattern, op.replacement)
        else:
            assert op.regex is not None
            data = op.regex.sub(op.replacement, data)
    return data


def process_inplace(
    source: SourceFile,
    ruleset: Ruleset,
    chunk_size: int,
    fsync: bool,
    allow_changing: bool,
) -> FileStats:
    """Process one file into a sibling temp file and atomically replace it."""

    src = source.path
    st = os.stat(src)
    if not allow_changing and (st.st_size != source.size or st.st_mtime_ns != source.mtime_ns):
        raise ProcessingError(f"{src}: 扫描后文件已变化，已中止（可使用 --allow-changing 放行）")

    fd, tmp_name = tempfile.mkstemp(prefix=".logreduce-", dir=str(src.parent))
    tmp = Path(tmp_name)
    try:
        out = os.fdopen(fd, "wb", buffering=0)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise

    try:
        with out:
            expected = None if allow_changing else (source.size, source.mtime_ns)
            stats = process_file(
                src,
                out,
                ruleset,
                chunk_size,
                expected=expected,
                check_change=not allow_changing,
            )
            out.flush()
            if fsync:
                os.fsync(out.fileno())
        if not allow_changing:
            current = os.stat(src)
            if current.st_size != st.st_size or current.st_mtime_ns != st.st_mtime_ns:
                raise ProcessingError(f"{src}: 文件在处理期间被修改，已中止")
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        os.replace(tmp, src)
        if fsync:
            fsync_dir(src.parent)
        return stats
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def process_inplace_batch(
    batch: Sequence[SourceFile],
    ruleset: Ruleset,
    chunk_size: int,
    fsync: bool,
    allow_changing: bool,
) -> list[FileStats]:
    return [
        process_inplace(source, ruleset, chunk_size, fsync, allow_changing)
        for source in batch
    ]


def process_range(
    source: SourceFile,
    shard_path: Path,
    start: int,
    end: int,
    ruleset: Ruleset,
    chunk_size: int,
    fsync: bool,
    allow_changing: bool,
) -> FileStats:
    """Process one byte range (line-aligned) of a file into a shard."""

    with open(shard_path, "wb", buffering=0) as out:
        expected = None if allow_changing else (source.size, source.mtime_ns)
        stats = process_file(
            source.path,
            out,
            ruleset,
            chunk_size,
            start=start,
            end=end,
            expected=expected,
            check_change=not allow_changing,
        )
        out.flush()
        if fsync:
            os.fsync(out.fileno())
    return stats


def process_batch(
    batch: Sequence[SourceFile],
    shard_path: Path,
    separator: bytes,
    ruleset: Ruleset,
    chunk_size: int,
    fsync: bool,
    allow_changing: bool,
) -> tuple[list[FileStats], int | None]:
    """Process a contiguous batch of files into one merged shard.

    Internal file boundaries use the same separator rule as the final merge:
    insert ``separator`` only when the previous non-empty file does not end
    with a newline.  If a file produces no output, any separator written for
    it is truncated away again.
    """

    stats_list: list[FileStats] = []
    last_byte: int | None = None
    shard_pos = 0

    with open(shard_path, "wb", buffering=0) as shard:
        for source in batch:
            src = source.path
            st = os.stat(src)
            if not allow_changing and (
                st.st_size != source.size or st.st_mtime_ns != source.mtime_ns
            ):
                raise ProcessingError(
                    f"{src}: 扫描后文件已变化，已中止（可使用 --allow-changing 放行）"
                )

            pos_before = shard_pos
            if last_byte is not None and last_byte != 10 and separator:
                _write_all_file(shard, separator)
                shard_pos += len(separator)

            expected = None if allow_changing else (source.size, source.mtime_ns)
            stats = process_file(
                src,
                shard,
                ruleset,
                chunk_size,
                expected=expected,
                check_change=not allow_changing,
            )
            stats_list.append(stats)

            if stats.bytes_out == 0:
                if shard_pos != pos_before:
                    shard.flush()
                    os.ftruncate(shard.fileno(), pos_before)
                    shard.seek(pos_before)
                    shard_pos = pos_before
            else:
                shard_pos += stats.bytes_out
                last_byte = stats.last_byte

        shard.flush()
        if fsync:
            os.fsync(shard.fileno())

    return stats_list, last_byte


# ---------------------------------------------------------------------------
# Multiprocessing workers
# ---------------------------------------------------------------------------

_WORKER_RULESET: Ruleset | None = None
_WORKER_CHUNK_SIZE: int = DEFAULT_CHUNK_SIZE
_WORKER_FSYNC: bool = True


def worker_init(rule_configs: Sequence[RuleConfig], chunk_size: int, fsync: bool) -> None:
    global _WORKER_RULESET, _WORKER_CHUNK_SIZE, _WORKER_FSYNC
    _WORKER_RULESET = Ruleset(rule_configs)
    _WORKER_CHUNK_SIZE = chunk_size
    _WORKER_FSYNC = fsync


def worker_inplace(task: tuple[SourceFile, bool]) -> FileStats:
    source, allow_changing = task
    ruleset = _WORKER_RULESET
    if ruleset is None:
        raise ProcessingError("内部错误: worker 未初始化")
    return process_inplace(
        source,
        ruleset,
        _WORKER_CHUNK_SIZE,
        _WORKER_FSYNC,
        allow_changing,
    )


def worker_inplace_batch(
    task: tuple[Sequence[SourceFile], bool],
) -> list[FileStats]:
    batch, allow_changing = task
    ruleset = _WORKER_RULESET
    if ruleset is None:
        raise ProcessingError("内部错误: worker 未初始化")
    return process_inplace_batch(
        batch,
        ruleset,
        _WORKER_CHUNK_SIZE,
        _WORKER_FSYNC,
        allow_changing,
    )


def worker_batch(
    task: tuple[Sequence[SourceFile], str, bytes, bool],
) -> tuple[list[FileStats], int | None]:
    batch, shard_path, separator, allow_changing = task
    ruleset = _WORKER_RULESET
    if ruleset is None:
        raise ProcessingError("内部错误: worker 未初始化")
    return process_batch(
        batch,
        Path(shard_path),
        separator,
        ruleset,
        _WORKER_CHUNK_SIZE,
        _WORKER_FSYNC,
        allow_changing,
    )


def worker_direct(
    task: tuple[DirectTask, bytes, bool],
) -> tuple[list[FileStats] | FileStats, int | None]:
    direct_task, separator, allow_changing = task
    ruleset = _WORKER_RULESET
    if ruleset is None:
        raise ProcessingError("内部错误: worker 未初始化")

    if direct_task.kind == "batch":
        assert direct_task.batch is not None
        return process_batch(
            direct_task.batch,
            direct_task.shard,
            separator,
            ruleset,
            _WORKER_CHUNK_SIZE,
            _WORKER_FSYNC,
            allow_changing,
        )

    assert direct_task.source is not None
    stats = process_range(
        direct_task.source,
        direct_task.shard,
        direct_task.start,
        direct_task.end,
        ruleset,
        _WORKER_CHUNK_SIZE,
        _WORKER_FSYNC,
        allow_changing,
    )
    return stats, (stats.last_byte if stats.bytes_out > 0 else None)
