"""Content checks for an exported public tree."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ConfigError, matches, strings


EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")


@dataclass(frozen=True)
class Violation:
    path: str
    line: int | None
    rule: str
    snippet: str

    def __str__(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else self.path
        return f"{location}: {self.rule}: {self.snippet}"


def _rules(config: dict[str, Any], key: str) -> list[tuple[str, re.Pattern[str], list[str]]]:
    result = []
    for index, entry in enumerate(config.get(key, []), 1):
        if not isinstance(entry, dict) or not isinstance(entry.get("pattern"), str):
            raise ConfigError(f"checks.{key}[{index}] needs a pattern")
        try:
            pattern = re.compile(entry["pattern"])
        except re.error as exc:
            raise ConfigError(f"Invalid checks.{key}[{index}] pattern: {exc}") from exc
        result.append((str(entry.get("name", f"{key} {index}")), pattern,
                       strings(entry, "allow")))
    return result


def check_tree(root: Path, config: dict[str, Any], *, run_commands: bool = True) -> list[Violation]:
    violations: list[Violation] = []
    max_size = config.get("max_file_size", 50_000_000)
    if not isinstance(max_size, int) or max_size < 0:
        raise ConfigError("checks.max_file_size must be a nonnegative integer")
    forbidden_paths = strings(config, "forbidden_paths")
    regex_rules = _rules(config, "regex")
    for index, pattern in enumerate(strings(config, "forbidden_regexes"), 1):
        try:
            regex_rules.append((f"forbidden regex {index}", re.compile(pattern), []))
        except re.error as exc:
            raise ConfigError(f"Invalid forbidden regex {index}: {exc}") from exc
    script_rules = _rules(config, "scripts")
    names = [re.compile(re.escape(name)) for name in strings(config, "name_patterns")]
    email_allow = strings(config, "email_allow")
    name_allow = strings(config, "name_allow")
    charset_allow = strings(config, "charset_allow")
    utf8_only = config.get("utf8_only", False)
    if not isinstance(utf8_only, bool):
        raise ConfigError("checks.utf8_only must be true or false")

    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            violations.append(Violation(rel, None, "symlink", "symbolic link is not allowed"))
            continue
        if not path.is_file():
            continue
        if matches(rel, forbidden_paths):
            violations.append(Violation(rel, None, "forbidden path", rel))
        size = path.stat().st_size
        if size > max_size:
            violations.append(Violation(rel, None, "file size", f"{size} bytes exceeds {max_size}"))
        raw = path.read_bytes()
        if b"\0" in raw:
            continue
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            if utf8_only and not matches(rel, charset_allow):
                violations.append(Violation(rel, None, "charset", "text is not UTF-8"))
            continue
        for number, line in enumerate(lines, 1):
            snippet = line.strip()[:160]
            for name, pattern, allow in (*regex_rules, *script_rules):
                if not matches(rel, allow) and pattern.search(line):
                    violations.append(Violation(rel, number, name, snippet))
            if not matches(rel, email_allow) and EMAIL.search(line):
                violations.append(Violation(rel, number, "email", snippet))
            if not matches(rel, name_allow) and any(p.search(line) for p in names):
                violations.append(Violation(rel, number, "name", snippet))

    if run_commands:
        for index, entry in enumerate(config.get("commands", []), 1):
            if not isinstance(entry, dict):
                raise ConfigError(f"checks.commands[{index}] must be a table")
            argv = strings(entry, "argv")
            if not argv:
                raise ConfigError(f"checks.commands[{index}].argv must not be empty")
            name = str(entry.get("name", f"command {index}"))
            try:
                result = subprocess.run(argv, cwd=root, text=True, capture_output=True, check=False)
                if result.returncode:
                    detail = (result.stderr or result.stdout).strip()[-300:]
                    violations.append(Violation(".", None, name, detail or f"exit {result.returncode}"))
            except OSError as exc:
                violations.append(Violation(".", None, name, str(exc)))
    return violations
