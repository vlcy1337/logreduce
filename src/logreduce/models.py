"""Shared data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SourceFile:
    """A discovered input file.

    ``aliases`` contains additional hard-link paths that point at the same
    inode.  The content is read/merged once, but every path is removed after a
    successful run.
    """

    path: Path
    rel: str
    size: int
    mtime_ns: int
    dev: int = 0
    ino: int = 0
    aliases: list[Path] = field(default_factory=list)

    @property
    def all_paths(self) -> list[Path]:
        return [self.path, *self.aliases]


@dataclass
class DirectTask:
    """A unit of work for direct mode.

    A ``batch`` task processes several files into one shard, inserting
    separators internally.  A ``range`` task processes a byte range of one
    file; consecutive ranges of the same file share ``group_id`` so the final
    merge does not insert separators between them.
    """

    kind: str  # "batch" | "range"
    group_id: int
    shard: Path
    batch: list[SourceFile] | None = None
    source: SourceFile | None = None
    start: int = 0
    end: int = 0
    can_split: bool = True
