"""Input discovery: glob matching, symlink policy, hard-link deduplication."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Sequence
from pathlib import Path

from .config import InputConfig
from .errors import DiscoveryError
from .models import SourceFile


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a POSIX-style glob (supporting ``**``) to a regex.

    ``*`` and ``?`` never cross ``/``.  ``**/`` matches zero or more
    directories, so ``**/*.log`` also matches top-level ``app.log``.
    """

    pattern = pattern.replace("\\", "/")
    while pattern.startswith("./"):
        pattern = pattern[2:]
    pattern = pattern.lstrip("/")

    out: list[str] = ["^"]
    i = 0
    n = len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                i += 2
                if i < n and pattern[i] == "/":
                    out.append("(?:.*/)?")
                    i += 1
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
                i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        elif char == "[":
            j = i + 1
            if j < n and pattern[j] in "!^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append(r"\[")
                i += 1
            else:
                content = pattern[i + 1 : j]
                if content.startswith("!"):
                    content = "^" + content[1:]
                elif content.startswith("^"):
                    pass
                out.append("[" + content + "]")
                i = j + 1
        else:
            out.append(re.escape(char))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def discover(
    log_dir: Path,
    cfg: InputConfig,
    output: Path | None,
    sort: str = "path",
    skip_paths: Sequence[Path] = (),
) -> list[SourceFile]:
    if not log_dir.exists():
        raise DiscoveryError(f"日志目录不存在: {log_dir}")
    if not log_dir.is_dir():
        raise DiscoveryError(f"日志路径不是目录: {log_dir}")

    try:
        includes = [glob_to_regex(p) for p in cfg.include]
        excludes = [glob_to_regex(p) for p in cfg.exclude]
    except re.error as exc:
        raise DiscoveryError(f"include/exclude glob 无效: {exc}") from exc
    output_resolved = output.resolve(strict=False) if output is not None else None
    skip_resolved = {path.resolve(strict=False) for path in skip_paths}

    candidates: list[SourceFile] = []
    seen_dirs: set[tuple[int, int]] = set()

    def on_walk_error(exc: OSError) -> None:
        raise DiscoveryError(f"遍历日志目录失败: {exc}")

    for root, dirs, files in os.walk(
        log_dir, followlinks=cfg.follow_symlinks, onerror=on_walk_error
    ):
        if cfg.follow_symlinks:
            try:
                root_stat = os.stat(root)
            except OSError as exc:
                raise DiscoveryError(f"无法读取目录状态 {root}: {exc}") from exc
            key = (root_stat.st_dev, root_stat.st_ino)
            if key in seen_dirs:
                dirs[:] = []
                continue
            seen_dirs.add(key)
            dirs[:] = [d for d in dirs if not d.startswith(".logreduce-")]
        else:
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".logreduce-")
                and not os.path.islink(os.path.join(root, d))
            ]
        dirs.sort()
        for name in files:
            path = Path(root) / name
            if name.startswith(".logreduce-"):
                continue
            try:
                lst = os.lstat(path)
            except OSError as exc:
                raise DiscoveryError(f"无法读取文件状态 {path}: {exc}") from exc
            if stat.S_ISLNK(lst.st_mode):
                # Symlink files are never processed: in-place mode would
                # replace the link rather than its target, and deleting a link
                # can be surprising.  Symlinked directories are controlled by
                # input.follow_symlinks.
                continue
            if not stat.S_ISREG(lst.st_mode):
                continue

            rel = path.relative_to(log_dir).as_posix()
            if not any(regex.match(rel) for regex in includes):
                continue
            if any(regex.match(rel) for regex in excludes):
                continue

            if output_resolved is not None or skip_resolved:
                try:
                    resolved = path.resolve(strict=False)
                except OSError as exc:
                    raise DiscoveryError(f"无法解析路径 {path}: {exc}") from exc
                if output_resolved is not None and resolved == output_resolved:
                    continue
                if resolved in skip_resolved:
                    continue

            if not os.access(path, os.R_OK):
                raise DiscoveryError(f"没有读取权限: {path}")

            candidates.append(
                SourceFile(
                    path=path,
                    rel=rel,
                    size=lst.st_size,
                    mtime_ns=lst.st_mtime_ns,
                    dev=lst.st_dev,
                    ino=lst.st_ino,
                )
            )

    if sort == "path":
        candidates.sort(key=lambda item: item.rel)
    elif sort == "mtime":
        candidates.sort(key=lambda item: (item.mtime_ns, item.rel))
    # "none": preserve os.walk order

    seen: dict[tuple[int, int], SourceFile] = {}
    result: list[SourceFile] = []
    for item in candidates:
        key = (item.dev, item.ino)
        existing = seen.get(key)
        if existing is not None:
            existing.aliases.append(item.path)
            continue
        seen[key] = item
        result.append(item)

    return result
