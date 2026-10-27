"""Statistics collected while processing log files."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FileStats:
    source: str
    bytes_in: int = 0
    bytes_out: int = 0
    lines_in: int = 0
    lines_out: int = 0
    lines_dropped: int = 0
    last_byte: int | None = None
    elapsed: float = 0.0


@dataclass
class RunStats:
    mode: str = "inplace"
    output: str = ""
    files: list[FileStats] = field(default_factory=list)
    duration: float = 0.0
    output_bytes: int | None = None

    def add_file(self, stats: FileStats) -> None:
        self.files.append(stats)

    def extend(self, stats: list[FileStats]) -> None:
        self.files.extend(stats)

    @property
    def files_processed(self) -> int:
        return len(self.files)

    @property
    def files_empty_output(self) -> int:
        return sum(1 for f in self.files if f.bytes_out == 0)

    @property
    def bytes_in(self) -> int:
        return sum(f.bytes_in for f in self.files)

    @property
    def bytes_out(self) -> int:
        if self.output_bytes is not None:
            return self.output_bytes
        return sum(f.bytes_out for f in self.files)

    @property
    def lines_in(self) -> int:
        return sum(f.lines_in for f in self.files)

    @property
    def lines_out(self) -> int:
        return sum(f.lines_out for f in self.files)

    @property
    def lines_dropped(self) -> int:
        return sum(f.lines_dropped for f in self.files)

    @property
    def throughput_mib_s(self) -> float:
        if self.duration <= 0:
            return 0.0
        return self.bytes_in / self.duration / (1024 * 1024)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "output": self.output,
            "files_processed": self.files_processed,
            "files_empty_output": self.files_empty_output,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "content_bytes_out": sum(f.bytes_out for f in self.files),
            "lines_in": self.lines_in,
            "lines_out": self.lines_out,
            "lines_dropped": self.lines_dropped,
            "duration_s": round(self.duration, 6),
            "throughput_mib_s": round(self.throughput_mib_s, 3),
        }

    def format_human(self) -> str:
        return "\n".join(
            [
                f"处理完成: {self.files_processed:,} 个文件"
                f"（其中 {self.files_empty_output:,} 个文件处理结果为空）",
                f"输入: {human_bytes(self.bytes_in)} / {self.lines_in:,} 行",
                f"输出: {human_bytes(self.bytes_out)} / {self.lines_out:,} 行",
                f"丢弃: {self.lines_dropped:,} 行",
                f"耗时: {self.duration:.3f} s，吞吐: {self.throughput_mib_s:.2f} MiB/s",
                f"输出文件: {self.output}",
            ]
        )

    def format_short(self) -> str:
        return (
            f"完成: {self.files_processed:,} 个文件，"
            f"输入 {human_bytes(self.bytes_in)} / {self.lines_in:,} 行，"
            f"输出 {human_bytes(self.bytes_out)} / {self.lines_out:,} 行，"
            f"丢弃 {self.lines_dropped:,} 行，"
            f"耗时 {self.duration:.3f} s；输出文件: {self.output}"
        )


def human_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    size = float(value)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PiB"
