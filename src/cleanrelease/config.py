"""Configuration loading and matching helpers."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


class ConfigError(ValueError):
    """A configuration value is missing or invalid."""


def load(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot load {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("Configuration must be a TOML table")
    return data


def table(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def strings(config: dict[str, Any], name: str) -> list[str]:
    value = config.get(name, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{name} must be a list of strings")
    return value


def matches(path: str, patterns: list[str]) -> bool:
    """Match a file or a directory prefix with shell style patterns."""
    for pattern in patterns:
        prefix = pattern.rstrip("/")
        if path == prefix or path.startswith(prefix + "/"):
            return True
        if fnmatch.fnmatchcase(path, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]):
            return True
    return False


EXAMPLE = '''# Run from the source Git repository, or pass --repo.
[source]
ref = "HEAD"

[public]
remote = "" # Git URL or configured remote name
branch = "main"
history = "orphan" # "orphan" or "keep"
force = false
exclude = ["cleanrelease.toml", "notes/", "data/", "outputs/", "docs_translated/"]
author_name = "Release Bot"
author_email = "mkj69@users.noreply.github.com"

[checks]
max_file_size = 50000000
utf8_only = true
charset_allow = []
forbidden_paths = ["**/*_results.md", "outputs/"]
forbidden_regexes = ["/private/workspace"]
email_allow = ["pyproject.toml", "CITATION.cff"]
name_patterns = ["Example Author"]
name_allow = ["CITATION.cff"]

[[checks.scripts]]
name = "CJK"
pattern = "[\\u3400-\\u9fff\\uf900-\\ufaff]"
allow = ["docs/examples/**"]

# [[checks.regex]]
# name = "private marker"
# pattern = "secret-[0-9]+"
# allow = ["tests/fixtures/**"]

# [[checks.commands]]
# name = "strict docs"
# argv = ["mkdocs", "build", "--strict"]

# [test_public_tree]
# enabled = true
# command = ["python", "-m", "pytest", "-q"]
# allow = [] # Extra import roots for environment-specific test dependencies

# [public.strip_json]
# enabled = true
# paths = ["data/public/**"]
# key_pattern = "^private_"

[archive]
remote = ""
exclude_refs = ["refs/heads/worktree-*", "refs/tags/temporary-*"]
snapshot_paths = ["notes/"]
snapshot_branch = "archive-snapshot"
snapshot_from = "HEAD"
max_file_size = 50000000

[pages]
enabled = false
build = ["mkdocs", "build", "--strict", "-d", "site"]
output = "site"
branch = "gh-pages"

[github]
repo = "" # owner/repo; empty skips GitHub settings
visibility = "" # "public" or "private"
discussions = false
pages = false
homepage = ""
topics = []

[install_check]
smoke = [["python", "-c", "import cleanrelease"], ["cleanrelease", "--help"]]
'''
