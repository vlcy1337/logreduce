"""Rule compilation and line processing.

All matching is performed on ``bytes``.  Patterns from the TOML file are
UTF-8 encoded once at startup; file contents are never decoded, so arbitrary
binary data and invalid UTF-8 are handled losslessly.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .config import RuleConfig
from .errors import ConfigError

_FLAG_MAP = {
    "i": re.IGNORECASE,
    "m": re.MULTILINE,
    "s": re.DOTALL,
    "x": re.VERBOSE,
}

LineFn = Callable[[bytes], "bytes | None"]


@dataclass(frozen=True)
class BulkOp:
    kind: str  # "literal" | "regex"
    pattern: bytes
    replacement: bytes
    regex: re.Pattern[bytes] | None


@dataclass(frozen=True)
class BulkOps:
    ops: tuple[BulkOp, ...]
    has_regex: bool


class Rule:
    __slots__ = (
        "index",
        "name",
        "action",
        "match_type",
        "source",
        "pattern",
        "flags",
        "regex",
        "replacement",
        "when",
        "when_type",
        "when_regex",
    )

    def __init__(self, cfg: RuleConfig) -> None:
        self.index = cfg.index
        self.name = cfg.name
        self.action = cfg.action
        self.match_type = cfg.match_type
        self.source = cfg.pattern
        self.flags = _parse_flags(cfg.flags)
        self.replacement = (
            cfg.replacement.encode("utf-8") if cfg.replacement is not None else None
        )
        self.when = cfg.when.encode("utf-8") if cfg.when is not None else None
        self.when_type = cfg.when_type
        self.when_regex: re.Pattern[bytes] | None = None

        try:
            self.pattern = cfg.pattern.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ConfigError(f"规则 {cfg.name}: pattern 无法编码为 UTF-8: {exc}") from exc

        self.regex: re.Pattern[bytes] | None = None
        if cfg.match_type == "regex":
            self.regex = _compile_regex(cfg.pattern, cfg.flags, f"规则 {cfg.name}.pattern")
        elif cfg.flags:
            raise ConfigError(f"规则 {cfg.name}: literal 模式不支持 flags")

        if cfg.action == "replace" and self.replacement is None:
            raise ConfigError(f"规则 {cfg.name}: replace 动作缺少 replacement")

        if (
            cfg.action == "replace"
            and cfg.match_type == "regex"
            and self.regex is not None
            and self.replacement is not None
        ):
            try:
                self.regex.sub(self.replacement, b"")
            except re.error as exc:
                raise ConfigError(f"规则 {cfg.name}: replacement 无效: {exc}") from exc

        if cfg.when is not None:
            if cfg.when_type == "regex":
                self.when_regex = _compile_regex(
                    cfg.when, cfg.when_flags, f"规则 {cfg.name}.when"
                )
            elif cfg.when_flags:
                raise ConfigError(f"规则 {cfg.name}: literal when 不支持 flags")


class Ruleset:
    """A compiled, ordered pipeline of rules."""

    __slots__ = ("rules", "fns", "has_filters", "bulk_ops")

    def __init__(self, configs: Sequence[RuleConfig]) -> None:
        self.rules = tuple(Rule(cfg) for cfg in configs)
        self.fns: tuple[LineFn, ...] = tuple(_make_line_fn(rule) for rule in self.rules)
        self.has_filters = any(rule.action in ("keep", "drop") for rule in self.rules)
        self.bulk_ops = _build_bulk_ops(self.rules)

    def process_line(self, line: bytes) -> bytes | None:
        fns = self.fns
        for fn in fns:
            line = fn(line)
            if line is None:
                return None
        return line


def _parse_flags(flags: str) -> int:
    value = 0
    for char in flags:
        try:
            value |= _FLAG_MAP[char]
        except KeyError as exc:
            raise ConfigError(f"不支持的 regex flag: {char!r}") from exc
    return value


def _compile_regex(source: str, flags: str, where: str) -> re.Pattern[bytes]:
    try:
        pattern = source.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ConfigError(f"{where}: 无法编码为 UTF-8: {exc}") from exc
    try:
        return re.compile(pattern, _parse_flags(flags))
    except re.error as exc:
        raise ConfigError(f"{where}: 正则无效: {exc}") from exc


def _make_line_fn(rule: Rule) -> LineFn:
    if rule.action == "drop":
        if rule.match_type == "literal":
            pattern = rule.pattern

            def fn(line: bytes, pattern: bytes = pattern) -> bytes | None:
                return None if pattern in line else line

        else:
            regex = rule.regex
            assert regex is not None

            def fn(line: bytes, regex: re.Pattern[bytes] = regex) -> bytes | None:
                return None if regex.search(line) else line

        return fn

    if rule.action == "keep":
        if rule.match_type == "literal":
            pattern = rule.pattern

            def fn(line: bytes, pattern: bytes = pattern) -> bytes | None:
                return line if pattern in line else None

        else:
            regex = rule.regex
            assert regex is not None

            def fn(line: bytes, regex: re.Pattern[bytes] = regex) -> bytes | None:
                return line if regex.search(line) else None

        return fn

    # replace
    assert rule.replacement is not None
    replacement = rule.replacement
    if rule.match_type == "literal":
        pattern = rule.pattern
        if rule.when is None:
            def fn(line: bytes, pattern: bytes = pattern, replacement: bytes = replacement) -> bytes:
                return line.replace(pattern, replacement)
        elif rule.when_type == "literal":
            when = rule.when

            def fn(
                line: bytes,
                pattern: bytes = pattern,
                replacement: bytes = replacement,
                when: bytes = when,
            ) -> bytes:
                if when not in line:
                    return line
                return line.replace(pattern, replacement)
        else:
            when_regex = rule.when_regex
            assert when_regex is not None

            def fn(
                line: bytes,
                pattern: bytes = pattern,
                replacement: bytes = replacement,
                when_regex: re.Pattern[bytes] = when_regex,
            ) -> bytes:
                if when_regex.search(line) is None:
                    return line
                return line.replace(pattern, replacement)

        return fn

    regex = rule.regex
    assert regex is not None
    if rule.when is None:
        def fn(line: bytes, regex: re.Pattern[bytes] = regex, replacement: bytes = replacement) -> bytes:
            return regex.sub(replacement, line)
    elif rule.when_type == "literal":
        when = rule.when

        def fn(
            line: bytes,
            regex: re.Pattern[bytes] = regex,
            replacement: bytes = replacement,
            when: bytes = when,
        ) -> bytes:
            if when not in line:
                return line
            return regex.sub(replacement, line)
    else:
        when_regex = rule.when_regex
        assert when_regex is not None

        def fn(
            line: bytes,
            regex: re.Pattern[bytes] = regex,
            replacement: bytes = replacement,
            when_regex: re.Pattern[bytes] = when_regex,
        ) -> bytes:
            if when_regex.search(line) is None:
                return line
            return regex.sub(replacement, line)

    return fn


def _build_bulk_ops(rules: Sequence[Rule]) -> BulkOps | None:
    """Return a bulk pipeline when whole-chunk substitution is provably safe.

    Conditions:
      * every rule is a ``replace`` without a ``when`` condition;
      * replacements never insert CR/LF (otherwise later rules would see
        different line boundaries than in strict line mode);
      * literal patterns contain no CR/LF;
      * every regex is line-local and cannot match an empty string;
      * regexes get MULTILINE so ``^``/``$`` still behave per line.

    Regexes that can consume CR/LF are excluded.  At runtime, a file that
    contains CRLF switches to strict line mode before any regex is applied.
    """

    ops: list[BulkOp] = []
    has_regex = False
    for rule in rules:
        if rule.action != "replace" or rule.when is not None:
            return None
        assert rule.replacement is not None
        if b"\n" in rule.replacement or b"\r" in rule.replacement:
            return None
        if rule.match_type == "literal":
            if b"\n" in rule.pattern or b"\r" in rule.pattern:
                return None
            ops.append(BulkOp("literal", rule.pattern, rule.replacement, None))
            continue

        if _replacement_can_insert_newline(rule.replacement):
            return None
        has_regex = True
        assert rule.regex is not None
        if not _regex_bulk_safe(rule.source, rule.flags, rule.regex):
            return None
        bulk_regex = re.compile(rule.pattern, rule.flags | re.MULTILINE)
        ops.append(BulkOp("regex", rule.pattern, rule.replacement, bulk_regex))

    if not ops:
        return None
    return BulkOps(tuple(ops), has_regex)


def _replacement_can_insert_newline(replacement: bytes) -> bool:
    """Whether a regex replacement template can expand to CR/LF.

    Python's ``re.sub`` interprets escapes such as ``\\n`` and ``\\x0a`` in
    the replacement template.  If a replacement inserts a newline, running
    later rules over a whole chunk would see different line boundaries than
    strict line mode, so the bulk path must be disabled.
    """

    if b"\n" in replacement or b"\r" in replacement:
        return True

    i = 0
    n = len(replacement)
    while i < n:
        if replacement[i] != 0x5C:  # backslash
            i += 1
            continue
        i += 1
        if i >= n:
            break
        esc = replacement[i]
        if esc in (0x6E, 0x72):  # n, r
            return True
        if esc == 0x78:  # x
            if i + 2 >= n:
                return True
            try:
                code = int(replacement[i + 1 : i + 3], 16)
            except ValueError:
                return True
            if code in (10, 13):
                return True
            i += 3
            continue
        if esc == 0x30:  # 0, octal escape
            j = i
            count = 0
            while j < n and count < 3 and 0x30 <= replacement[j] <= 0x37:
                j += 1
                count += 1
            try:
                code = int(replacement[i:j], 8)
            except ValueError:
                return True
            if code in (10, 13):
                return True
            i = j
            continue
        if esc in (0x75, 0x55, 0x4E):  # u, U, N
            return True
        i += 1
    return False


def _regex_bulk_safe(source: str, flags: int, compiled: re.Pattern[bytes]) -> bool:
    if flags & (re.VERBOSE | re.DOTALL):
        return False
    if _has_inline_flag(source, "s") or _has_inline_flag(source, "x"):
        return False
    # Inline flag disabling (e.g. (?-m)) could change anchor semantics.
    if "(?-" in source:
        return False
    # A zero-width match at arbitrary positions behaves differently at chunk
    # boundaries; play it safe.
    if compiled.match(b"") is not None:
        return False
    if _has_absolute_anchor(source):
        return False
    if _can_match_newline(source):
        return False
    return True


def _has_inline_flag(source: str, flag: str) -> bool:
    return (
        re.search(
            r"\(\?[A-Za-z-]*" + re.escape(flag) + r"[A-Za-z-]*[:)]",
            source,
        )
        is not None
    )


def _has_absolute_anchor(source: str) -> bool:
    return "\\A" in source or "\\Z" in source or "\\z" in source


def _can_match_newline(pattern: str) -> bool:
    """Conservatively decide whether a regex source can consume CR or LF.

    This is intentionally conservative: returning True only disables the
    whole-chunk fast path, never changes correctness.
    """

    i = 0
    n = len(pattern)
    while i < n:
        char = pattern[i]

        if char == "\\":
            i += 1
            if i >= n:
                return True
            esc = pattern[i]
            if esc in "nrsWD":
                return True
            if esc == "S":
                i += 1
                continue
            if esc == "x":
                if i + 2 >= n:
                    return True
                try:
                    code = int(pattern[i + 1 : i + 3], 16)
                except ValueError:
                    return True
                if code in (10, 13):
                    return True
                i += 3
                continue
            if esc == "u":
                if i + 4 >= n:
                    return True
                try:
                    code = int(pattern[i + 1 : i + 5], 16)
                except ValueError:
                    return True
                if code in (10, 13):
                    return True
                i += 5
                continue
            if esc == "U":
                if i + 8 >= n:
                    return True
                try:
                    code = int(pattern[i + 1 : i + 9], 16)
                except ValueError:
                    return True
                if code in (10, 13):
                    return True
                i += 9
                continue
            if esc == "N":
                return True
            if esc in "01234567":
                j = i
                count = 0
                while j < n and count < 3 and pattern[j] in "01234567":
                    j += 1
                    count += 1
                if int(pattern[i:j], 8) in (10, 13):
                    return True
                i = j
                continue
            # \A \Z \z \b \B \w \d \t \f \v \a \e and escaped literals
            i += 1
            continue

        if char == "[":
            i, can_match = _scan_class(pattern, i)
            if can_match:
                return True
            continue

        if char in "\n\r":
            return True

        if char == ".":
            # Without DOTALL, '.' cannot consume LF; CRLF files fall back to
            # strict line mode at runtime.
            i += 1
            continue

        i += 1

    return False


def _scan_class(pattern: str, start: int) -> tuple[int, bool]:
    """Scan a character class starting at ``pattern[start] == '['``.

    Returns ``(index_after_class, can_match_cr_or_lf)``.  On anything
    ambiguous, returns True.
    """

    n = len(pattern)
    i = start + 1
    if i < n and pattern[i] == "^":
        end = _find_class_end(pattern, i + 1)
        return (end + 1 if end is not None else n, True)

    tokens: list[tuple[str, int | None]] = []
    first = True
    closed = False
    while i < n:
        char = pattern[i]
        if char == "]" and not first:
            i += 1
            closed = True
            break
        first = False

        if char == "\\":
            i += 1
            if i >= n:
                return (n, True)
            esc = pattern[i]
            if esc in "nrsWDN":
                return (n, True)
            if esc == "x":
                if i + 2 >= n:
                    return (n, True)
                try:
                    code = int(pattern[i + 1 : i + 3], 16)
                except ValueError:
                    return (n, True)
                if code in (10, 13):
                    return (n, True)
                tokens.append(("char", code))
                i += 3
                continue
            if esc == "u":
                if i + 4 >= n:
                    return (n, True)
                try:
                    code = int(pattern[i + 1 : i + 5], 16)
                except ValueError:
                    return (n, True)
                if code in (10, 13):
                    return (n, True)
                tokens.append(("char", code))
                i += 5
                continue
            if esc == "U":
                if i + 8 >= n:
                    return (n, True)
                try:
                    code = int(pattern[i + 1 : i + 9], 16)
                except ValueError:
                    return (n, True)
                if code in (10, 13):
                    return (n, True)
                tokens.append(("char", code))
                i += 9
                continue
            if esc in "01234567":
                j = i
                count = 0
                while j < n and count < 3 and pattern[j] in "01234567":
                    j += 1
                    count += 1
                code = int(pattern[i:j], 8)
                if code in (10, 13):
                    return (n, True)
                tokens.append(("char", code))
                i = j
                continue
            if esc in "wdS":
                tokens.append(("safe", None))
                i += 1
                continue
            if esc == "b":
                # Inside a character class \b means backspace.
                tokens.append(("char", 8))
                i += 1
                continue
            if esc in "tfvae":
                tokens.append(("char", {"t": 9, "f": 12, "v": 11, "a": 7, "e": 27}[esc]))
                i += 1
                continue
            tokens.append(("char", ord(esc)))
            i += 1
            continue

        if char in "\n\r":
            return (n, True)
        tokens.append(("char", ord(char)))
        i += 1

    if not closed:
        return (n, True)

    j = 0
    while j < len(tokens):
        kind, code = tokens[j]
        if kind == "safe":
            # A category endpoint in a range is too subtle to reason about.
            if j + 1 < len(tokens) and tokens[j + 1] == ("char", ord("-")):
                return (n, True)
            j += 1
            continue
        assert code is not None
        if (
            j + 2 < len(tokens)
            and tokens[j + 1] == ("char", ord("-"))
            and tokens[j + 2][0] == "safe"
        ):
            return (n, True)
        if (
            j + 2 < len(tokens)
            and tokens[j + 1] == ("char", ord("-"))
            and tokens[j + 2][0] == "char"
        ):
            high = tokens[j + 2][1]
            assert high is not None
            if code <= 10 <= high or code <= 13 <= high:
                return (n, True)
            j += 3
            continue
        if code in (10, 13):
            return (n, True)
        j += 1

    return (i, False)


def _find_class_end(pattern: str, start: int) -> int | None:
    n = len(pattern)
    i = start
    first = True
    while i < n:
        char = pattern[i]
        if char == "\\":
            i += 2
            first = False
            continue
        if char == "]" and not first:
            return i
        first = False
        i += 1
    return None
