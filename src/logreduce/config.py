"""TOML configuration parsing and validation."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from .errors import ConfigError

SUPPORTED_VERSION = 1
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
MIN_CHUNK_SIZE = 4096
MAX_CHUNK_SIZE = 1024**3
RULE_ACTIONS = frozenset({"keep", "drop", "replace"})
MATCH_TYPES = frozenset({"literal", "regex"})
MODES = frozenset({"inplace", "direct"})
SORTS = frozenset({"path", "mtime", "none"})
FLAG_CHARS = frozenset("imsx")

_CHUNK_MULTIPLIERS = {
    "": 1,
    "b": 1,
    "k": 1000,
    "kb": 1000,
    "kib": 1024,
    "m": 1000**2,
    "mb": 1000**2,
    "mib": 1024**2,
    "g": 1000**3,
    "gb": 1000**3,
    "gib": 1024**3,
}
_CHUNK_RE = re.compile(r"^\s*(\d+)\s*([A-Za-z]*)\s*$")


@dataclass(frozen=True)
class InputConfig:
    include: tuple[str, ...] = ("**/*.log",)
    exclude: tuple[str, ...] = ()
    follow_symlinks: bool = False


@dataclass(frozen=True)
class OutputConfig:
    path: str | None = None
    separator: str = "\n"
    delete_sources: bool = True


@dataclass(frozen=True)
class EngineConfig:
    mode: str = "inplace"
    jobs: int = 0
    chunk_size: int = DEFAULT_CHUNK_SIZE
    fsync: bool = True
    sort: str = "path"
    allow_changing: bool = False


@dataclass(frozen=True)
class RuleConfig:
    index: int
    name: str
    action: str
    match_type: str
    pattern: str
    flags: str = ""
    replacement: str | None = None
    when: str | None = None
    when_type: str = "literal"
    when_flags: str = ""


@dataclass(frozen=True)
class Config:
    version: int
    input: InputConfig
    output: OutputConfig
    engine: EngineConfig
    rules: tuple[RuleConfig, ...]
    source: Path | None = None


class _Parser:
    def __init__(self, data: Mapping[str, Any], source: Path | None) -> None:
        self.data = data
        self.source = source

    def fail(self, where: str, message: str) -> NoReturn:
        prefix = f"{self.source}: " if self.source is not None else ""
        raise ConfigError(f"{prefix}{where}: {message}")

    @staticmethod
    def _mapping(value: Any, where: str, fail: Any) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            fail(where, "必须是表（table）")
        return value

    def _check_keys(
        self, table: Mapping[str, Any], allowed: set[str] | frozenset[str], where: str
    ) -> None:
        unknown = sorted(set(table) - set(allowed))
        if unknown:
            self.fail(where, f"未知字段: {', '.join(unknown)}")

    def _bool(self, table: Mapping[str, Any], key: str, default: bool, where: str) -> bool:
        if key not in table:
            return default
        value = table[key]
        if type(value) is not bool:
            self.fail(f"{where}.{key}", "必须是布尔值 true/false")
        return value

    def _int(
        self,
        table: Mapping[str, Any],
        key: str,
        default: int,
        where: str,
        *,
        minimum: int | None = None,
    ) -> int:
        if key not in table:
            return default
        value = table[key]
        if type(value) is not int:
            self.fail(f"{where}.{key}", "必须是整数")
        if minimum is not None and value < minimum:
            self.fail(f"{where}.{key}", f"不能小于 {minimum}")
        return value

    def _str(
        self,
        table: Mapping[str, Any],
        key: str,
        default: str | None,
        where: str,
        *,
        allow_empty: bool = False,
    ) -> str | None:
        if key not in table:
            return default
        value = table[key]
        if not isinstance(value, str):
            self.fail(f"{where}.{key}", "必须是字符串")
        if not value and not allow_empty:
            self.fail(f"{where}.{key}", "不能为空")
        return value

    def _str_list(
        self, table: Mapping[str, Any], key: str, default: tuple[str, ...], where: str
    ) -> tuple[str, ...]:
        if key not in table:
            return default
        value = table[key]
        if not isinstance(value, list):
            self.fail(f"{where}.{key}", "必须是字符串数组")
        result: list[str] = []
        for i, item in enumerate(value):
            if not isinstance(item, str):
                self.fail(f"{where}.{key}[{i}]", "必须是字符串")
            if not item:
                self.fail(f"{where}.{key}[{i}]", "不能为空")
            result.append(item)
        return tuple(result)

    def parse(self) -> Config:
        data = self._mapping(self.data, "根配置", self.fail)
        self._check_keys(data, {"version", "input", "output", "engine", "rules"}, "根配置")

        version = self._int(data, "version", SUPPORTED_VERSION, "根配置", minimum=1)
        if version != SUPPORTED_VERSION:
            self.fail("根配置.version", f"不支持版本 {version}，当前支持 {SUPPORTED_VERSION}")

        input_cfg = self._parse_input(data.get("input", {}))
        output_cfg = self._parse_output(data.get("output", {}))
        engine_cfg = self._parse_engine(data.get("engine", {}))
        rules = self._parse_rules(data.get("rules"))

        return Config(
            version=version,
            input=input_cfg,
            output=output_cfg,
            engine=engine_cfg,
            rules=rules,
            source=self.source,
        )

    def _parse_input(self, value: Any) -> InputConfig:
        table = self._mapping(value, "[input]", self.fail)
        self._check_keys(table, {"include", "exclude", "follow_symlinks"}, "[input]")
        include = self._str_list(table, "include", ("**/*.log",), "[input]")
        if not include:
            self.fail("[input].include", "至少需要一个 glob 模式")
        exclude = self._str_list(table, "exclude", (), "[input]")
        follow_symlinks = self._bool(table, "follow_symlinks", False, "[input]")
        return InputConfig(include=include, exclude=exclude, follow_symlinks=follow_symlinks)

    def _parse_output(self, value: Any) -> OutputConfig:
        table = self._mapping(value, "[output]", self.fail)
        self._check_keys(table, {"path", "separator", "delete_sources"}, "[output]")
        path = self._str(table, "path", None, "[output]")
        separator = self._str(table, "separator", "\n", "[output]", allow_empty=True)
        assert separator is not None
        delete_sources = self._bool(table, "delete_sources", True, "[output]")
        return OutputConfig(path=path, separator=separator, delete_sources=delete_sources)

    def _parse_engine(self, value: Any) -> EngineConfig:
        table = self._mapping(value, "[engine]", self.fail)
        self._check_keys(
            table,
            {"mode", "jobs", "chunk_size", "fsync", "sort", "allow_changing"},
            "[engine]",
        )
        mode = self._str(table, "mode", "inplace", "[engine]")
        assert mode is not None
        mode = mode.lower()
        if mode not in MODES:
            self.fail("[engine].mode", f"必须是 {', '.join(sorted(MODES))} 之一")
        jobs = self._int(table, "jobs", 0, "[engine]", minimum=0)
        chunk_value = table.get("chunk_size", DEFAULT_CHUNK_SIZE)
        chunk_size = self._chunk_size(chunk_value, "[engine].chunk_size")
        fsync = self._bool(table, "fsync", True, "[engine]")
        sort = self._str(table, "sort", "path", "[engine]")
        assert sort is not None
        sort = sort.lower()
        if sort not in SORTS:
            self.fail("[engine].sort", f"必须是 {', '.join(sorted(SORTS))} 之一")
        allow_changing = self._bool(table, "allow_changing", False, "[engine]")
        return EngineConfig(
            mode=mode,
            jobs=jobs,
            chunk_size=chunk_size,
            fsync=fsync,
            sort=sort,
            allow_changing=allow_changing,
        )

    def _chunk_size(self, value: Any, where: str) -> int:
        if type(value) is int:
            size = value
        elif isinstance(value, str):
            match = _CHUNK_RE.match(value)
            if match is None:
                self.fail(where, "格式无效，示例: 8MiB、64MB、1048576")
            number = int(match.group(1))
            unit = match.group(2).lower()
            multiplier = _CHUNK_MULTIPLIERS.get(unit)
            if multiplier is None:
                self.fail(where, f"不支持的单位 {unit!r}")
            size = number * multiplier
        else:
            self.fail(where, "必须是整数或带单位的字符串")
        if size < MIN_CHUNK_SIZE:
            self.fail(where, f"不能小于 {MIN_CHUNK_SIZE} 字节")
        if size > MAX_CHUNK_SIZE:
            self.fail(where, f"不能大于 {MAX_CHUNK_SIZE} 字节（1 GiB）")
        return size

    def _parse_rules(self, value: Any) -> tuple[RuleConfig, ...]:
        if value is None:
            self.fail("[[rules]]", "至少需要一条规则")
        if not isinstance(value, list):
            self.fail("[[rules]]", "必须使用 [[rules]] 数组表")
        if not value:
            self.fail("[[rules]]", "至少需要一条规则")
        return tuple(self._parse_rule(item, i) for i, item in enumerate(value))

    def _parse_rule(self, value: Any, index: int) -> RuleConfig:
        where = f"[[rules]][{index}]"
        table = self._mapping(value, where, self.fail)
        self._check_keys(
            table,
            {
                "name",
                "action",
                "match_type",
                "pattern",
                "flags",
                "replacement",
                "when",
                "when_type",
                "when_flags",
            },
            where,
        )

        name = self._str(table, "name", f"rule-{index + 1}", where)
        assert name is not None

        action = self._str(table, "action", None, where)
        if action is None:
            self.fail(f"{where}.action", "缺少必填字段")
        action = action.lower()
        if action not in RULE_ACTIONS:
            self.fail(f"{where}.action", f"必须是 {', '.join(sorted(RULE_ACTIONS))} 之一")

        match_type = self._str(table, "match_type", "regex", where)
        assert match_type is not None
        match_type = match_type.lower()
        if match_type not in MATCH_TYPES:
            self.fail(f"{where}.match_type", f"必须是 {', '.join(sorted(MATCH_TYPES))} 之一")

        pattern = self._str(table, "pattern", None, where)
        if pattern is None:
            self.fail(f"{where}.pattern", "缺少必填字段")
        if match_type == "literal" and ("\n" in pattern or "\r" in pattern):
            self.fail(f"{where}.pattern", "按行模式下字面量不能包含换行符")

        flags = self._str(table, "flags", "", where, allow_empty=True)
        assert flags is not None
        self._validate_flags(flags, f"{where}.flags")
        if match_type == "literal" and flags:
            self.fail(f"{where}.flags", "literal 模式不支持 flags")

        replacement: str | None = None
        if action == "replace":
            replacement = self._str(table, "replacement", None, where, allow_empty=True)
            if replacement is None:
                self.fail(f"{where}.replacement", "replace 动作必须提供 replacement（可以为空字符串）")
        elif "replacement" in table:
            self.fail(f"{where}.replacement", f"{action} 动作不应设置 replacement")

        when = self._str(table, "when", None, where)
        when_type = self._str(table, "when_type", "literal", where)
        assert when_type is not None
        when_type = when_type.lower()
        when_flags = self._str(table, "when_flags", "", where, allow_empty=True)
        assert when_flags is not None
        self._validate_flags(when_flags, f"{where}.when_flags")

        if action == "replace":
            if when is not None:
                if when_type not in MATCH_TYPES:
                    self.fail(
                        f"{where}.when_type",
                        f"必须是 {', '.join(sorted(MATCH_TYPES))} 之一",
                    )
                if when_type == "literal" and ("\n" in when or "\r" in when):
                    self.fail(f"{where}.when", "按行模式下字面量不能包含换行符")
                if when_type == "literal" and when_flags:
                    self.fail(f"{where}.when_flags", "literal when 不支持 flags")
            else:
                if "when_type" in table or "when_flags" in table:
                    self.fail(f"{where}.when", "设置 when_type/when_flags 时必须同时设置 when")
        else:
            if when is not None or "when_type" in table or "when_flags" in table:
                self.fail(f"{where}.when", f"{action} 动作不支持 when 条件")

        return RuleConfig(
            index=index,
            name=name,
            action=action,
            match_type=match_type,
            pattern=pattern,
            flags=flags,
            replacement=replacement,
            when=when,
            when_type=when_type,
            when_flags=when_flags,
        )

    def _validate_flags(self, flags: str, where: str) -> None:
        invalid = sorted(set(flags) - FLAG_CHARS)
        if invalid:
            self.fail(
                where,
                f"不支持的 flags: {', '.join(invalid)}；可用: i, m, s, x",
            )
        if len(set(flags)) != len(flags):
            self.fail(where, "flags 不能重复")


def parse_config(data: Mapping[str, Any], source: Path | None = None) -> Config:
    return _Parser(data, source).parse()


def load_config(path: Path | str) -> Config:
    config_path = Path(path).expanduser()
    try:
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在: {config_path}") from exc
    except IsADirectoryError as exc:
        raise ConfigError(f"配置文件是目录: {config_path}") from exc
    except PermissionError as exc:
        raise ConfigError(f"没有权限读取配置文件: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path}: TOML 解析失败: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {config_path}: {exc}") from exc
    return parse_config(data, source=config_path)
