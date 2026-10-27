"""Small POSIX file-system helpers used by the engine and merger."""

from __future__ import annotations

import errno
import os
from pathlib import Path

_COPY_FALLBACK_ERRNOS = {
    errno.EXDEV,
    errno.EINVAL,
    errno.ENOSYS,
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    getattr(errno, "ENOTSUP", errno.EINVAL),
}


def default_file_mode() -> int:
    """Return the mode a normal ``open(..., 'wb')`` would create."""

    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def fsync_dir(path: Path) -> None:
    """fsync a directory entry (best effort for unsupported filesystems)."""

    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL)):
            return
        raise
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL)):
                raise
    finally:
        os.close(fd)


def write_all(fd: int, data: bytes | memoryview) -> None:
    """Write all bytes to a raw descriptor, handling short writes."""

    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "write returned 0 bytes")
        view = view[written:]


def copy_fd(src_fd: int, dst_fd: int) -> int:
    """Copy the rest of ``src_fd`` to ``dst_fd``.

    Uses Linux copy_file_range (kernel-side copy) and falls back to a
    read/write loop for filesystems that do not support it.  Returns the
    number of bytes copied.
    """

    total = 0
    copy_file_range = getattr(os, "copy_file_range", None)
    if copy_file_range is not None:
        while True:
            try:
                copied = copy_file_range(src_fd, dst_fd, 1 << 30)
            except OSError as exc:
                if exc.errno in _COPY_FALLBACK_ERRNOS:
                    break
                raise
            if copied == 0:
                return total
            total += copied

    while True:
        data = os.read(src_fd, 1 << 20)
        if not data:
            return total
        write_all(dst_fd, data)
        total += len(data)
