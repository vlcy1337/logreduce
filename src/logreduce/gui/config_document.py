from __future__ import annotations

import tomllib
from typing import Any

from ..config import Config, ConfigError, parse_config


def parse_content(content: str) -> Config:
    try:
        raw = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc
    return parse_config(raw)


def parse_document(document: Any) -> Config:
    if not isinstance(document, dict):
        raise ConfigError("configuration document must be an object")
    return parse_config(document)


def config_to_document(config: Config) -> dict[str, Any]:
    rules: list[dict[str, Any]] = []
    for rule in config.rules:
        raw_rule: dict[str, Any] = {"name": rule.name, "filter": None, "replace": None}
        if rule.filter_mode is not None:
            raw_rule["filter"] = {
                "mode": rule.filter_mode,
                "pattern": rule.filter_pattern,
            }
        if rule.replace_pattern is not None:
            raw_rule["replace"] = {
                "pattern": rule.replace_pattern,
                "replacement": rule.replacement,
            }
        rules.append(raw_rule)
    return {
        "version": 1,
        "output": config.output,
        "encoding": config.encoding,
        "rules": rules,
    }


def serialize_config(config: Config) -> str:
    lines = [
        "version = 1",
        f"output = {_toml_string(config.output)}",
        f"encoding = {_toml_string(config.encoding)}",
    ]
    for rule in config.rules:
        lines.extend(["", "[[rules]]", f"name = {_toml_string(rule.name)}"])
        if rule.filter_mode is not None:
            lines.extend(
                [
                    "",
                    "[rules.filter]",
                    f"mode = {_toml_string(rule.filter_mode)}",
                    f"pattern = {_toml_string(rule.filter_pattern or '')}",
                ]
            )
        if rule.replace_pattern is not None:
            lines.extend(
                [
                    "",
                    "[rules.replace]",
                    f"pattern = {_toml_string(rule.replace_pattern)}",
                    f"replacement = {_toml_string(rule.replacement or '')}",
                ]
            )
    return "\n".join(lines) + "\n"


def _toml_string(value: str) -> str:
    escaped: list[str] = ['"']
    replacements = {
        "\b": r"\b",
        "\t": r"\t",
        "\n": r"\n",
        "\f": r"\f",
        "\r": r"\r",
        '"': r"\"",
        "\\": r"\\",
    }
    for character in value:
        replacement = replacements.get(character)
        if replacement is not None:
            escaped.append(replacement)
        elif ord(character) < 0x20 or ord(character) == 0x7F:
            escaped.append(f"\\u{ord(character):04X}")
        else:
            escaped.append(character)
    escaped.append('"')
    return "".join(escaped)
