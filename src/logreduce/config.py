from __future__ import annotations

import codecs
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


class ConfigError(ValueError):
    """Raised when the processing configuration is invalid."""


@dataclass(frozen=True, slots=True)
class RuleSpec:
    name: str
    filter_mode: str | None
    filter_pattern: str | None
    replace_pattern: str | None
    replacement: str | None


@dataclass(frozen=True, slots=True)
class Config:
    output: str
    encoding: str
    rules: tuple[RuleSpec, ...]


@dataclass(frozen=True, slots=True)
class CompiledRule:
    name: str
    include_matches: bool
    filter_re: re.Pattern[str] | None
    filter_literal: str | None
    replace_re: re.Pattern[str] | None
    replace_literal: str | None
    replacement: str | None
    replacement_literal: str | None


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as config_file:
            raw = tomllib.load(config_file)
    except OSError as exc:
        raise ConfigError(f"cannot read configuration {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> Config:
    allowed = {"version", "output", "encoding", "rules"}
    _reject_unknown(raw, allowed, "configuration")

    version = raw.get("version")
    if isinstance(version, bool) or version != 1:
        raise ConfigError("configuration 'version' must be the integer 1")

    output = raw.get("output")
    if not isinstance(output, str) or not output:
        raise ConfigError("configuration 'output' must be a non-empty string")
    normalized_output = _validate_output(output)

    encoding = raw.get("encoding", "utf-8")
    if not isinstance(encoding, str) or not encoding:
        raise ConfigError("configuration 'encoding' must be a non-empty string")
    try:
        codecs.lookup(encoding)
    except LookupError as exc:
        raise ConfigError(f"unknown text encoding: {encoding}") from exc

    raw_rules = raw.get("rules", [])
    if not isinstance(raw_rules, list):
        raise ConfigError("configuration 'rules' must be an array of tables")

    rules = tuple(
        _parse_rule(raw_rule, index)
        for index, raw_rule in enumerate(raw_rules, start=1)
    )
    compiled_config = Config(normalized_output, encoding, rules)
    compile_rules(compiled_config.rules)
    return compiled_config


def compile_rules(rules: tuple[RuleSpec, ...]) -> tuple[CompiledRule, ...]:
    compiled: list[CompiledRule] = []
    for rule in rules:
        try:
            filter_re = (
                re.compile(rule.filter_pattern)
                if rule.filter_pattern is not None
                else None
            )
            replace_re = (
                re.compile(rule.replace_pattern)
                if rule.replace_pattern is not None
                else None
            )
        except re.error as exc:
            raise ConfigError(f"invalid regex in rule {rule.name!r}: {exc}") from exc

        compiled.append(
            CompiledRule(
                name=rule.name,
                include_matches=rule.filter_mode == "include",
                filter_re=filter_re,
                filter_literal=_literal_pattern(rule.filter_pattern),
                replace_re=replace_re,
                replace_literal=_literal_pattern(rule.replace_pattern),
                replacement=rule.replacement,
                replacement_literal=(
                    rule.replacement
                    if rule.replacement is not None
                    and "\\" not in rule.replacement
                    else None
                ),
            )
        )
    return tuple(compiled)


def _parse_rule(raw_rule: Any, index: int) -> RuleSpec:
    if not isinstance(raw_rule, dict):
        raise ConfigError(f"rules[{index}] must be a table")
    _reject_unknown(raw_rule, {"name", "filter", "replace"}, f"rules[{index}]")

    name = raw_rule.get("name", f"rule {index}")
    if not isinstance(name, str) or not name:
        raise ConfigError(f"rules[{index}].name must be a non-empty string")

    filter_mode: str | None = None
    filter_pattern: str | None = None
    raw_filter = raw_rule.get("filter")
    if raw_filter is not None:
        if not isinstance(raw_filter, dict):
            raise ConfigError(f"rules[{index}].filter must be a table")
        _reject_unknown(
            raw_filter,
            {"mode", "pattern"},
            f"rules[{index}].filter",
        )
        filter_mode = raw_filter.get("mode")
        if filter_mode not in {"include", "exclude"}:
            raise ConfigError(
                f"rules[{index}].filter.mode must be 'include' or 'exclude'"
            )
        filter_pattern = raw_filter.get("pattern")
        if not isinstance(filter_pattern, str):
            raise ConfigError(
                f"rules[{index}].filter.pattern must be a string"
            )

    replace_pattern: str | None = None
    replacement: str | None = None
    raw_replace = raw_rule.get("replace")
    if raw_replace is not None:
        if not isinstance(raw_replace, dict):
            raise ConfigError(f"rules[{index}].replace must be a table")
        _reject_unknown(
            raw_replace,
            {"pattern", "replacement"},
            f"rules[{index}].replace",
        )
        replace_pattern = raw_replace.get("pattern")
        if not isinstance(replace_pattern, str):
            raise ConfigError(
                f"rules[{index}].replace.pattern must be a string"
            )
        replacement = raw_replace.get("replacement")
        if not isinstance(replacement, str):
            raise ConfigError(
                f"rules[{index}].replace.replacement must be a string"
            )

    if raw_filter is None and raw_replace is None:
        raise ConfigError(
            f"rules[{index}] must define at least one of 'filter' or 'replace'"
        )

    return RuleSpec(
        name=name,
        filter_mode=filter_mode,
        filter_pattern=filter_pattern,
        replace_pattern=replace_pattern,
        replacement=replacement,
    )


def _validate_output(output: str) -> str:
    path = PurePosixPath(output)
    raw_parts = output.split("/")
    if path.is_absolute() or not path.parts:
        raise ConfigError("configuration 'output' must be a relative path")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ConfigError("configuration 'output' must not contain '.' or '..'")
    if output.endswith("/"):
        raise ConfigError("configuration 'output' must name a file")
    return path.as_posix()


def _reject_unknown(
    value: dict[str, Any],
    allowed: set[str],
    location: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        fields = ", ".join(repr(field) for field in unknown)
        raise ConfigError(f"unknown field(s) in {location}: {fields}")


def _literal_pattern(pattern: str | None) -> str | None:
    if pattern is not None and pattern and re.escape(pattern) == pattern:
        return pattern
    return None
