"""Pre-release generated-file and post-release live-artifact gates."""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import ConfigError, matches, strings
from .gitops import ReleaseError, export_tree


def _patterns(settings: dict[str, Any], key: str) -> list[str]:
    patterns = strings(settings, key)
    if not patterns or any(not pattern or Path(pattern).is_absolute() or
                           ".." in Path(pattern).parts or ".git" in Path(pattern).parts
                           for pattern in patterns):
        raise ConfigError(f"generated.{key} must contain safe relative paths")
    return patterns


def _files(root: Path, patterns: list[str]) -> dict[str, Path]:
    return {path.relative_to(root).as_posix(): path for path in root.rglob("*")
            if matches(path.relative_to(root).as_posix(), patterns) and
            (path.is_file() or path.is_symlink()) and ".git" not in path.relative_to(root).parts}


def check_generated(repo: Path, commit: str, settings: dict[str, Any], temp: Path) -> None:
    if not settings:
        return
    patterns = _patterns(settings, "paths")
    reference = temp / "generated-reference"
    export_tree(repo, commit, reference, [])
    committed = _files(reference, patterns)
    current = _files(repo, patterns)
    if not committed and not current:
        raise ReleaseError("No generated files match generated.paths")
    differences = []
    for name in sorted(committed.keys() | current.keys()):
        old, new = committed.get(name), current.get(name)
        if not old or not new or old.is_symlink() or new.is_symlink() or \
                old.read_bytes() != new.read_bytes() or \
                bool(old.stat().st_mode & 0o111) != bool(new.stat().st_mode & 0o111):
            differences.append(name)
    if differences:
        raise ReleaseError("Generated files differ from release ref; commit before publishing: " +
                           ", ".join(differences))
    freshness = settings.get("freshness", [])
    if not isinstance(freshness, list) or not all(isinstance(rule, dict) for rule in freshness):
        raise ConfigError("generated.freshness must be an array of tables")
    for index, rule in enumerate(freshness, 1):
        outputs = _files(repo, _patterns(rule, "files"))
        sources = _files(repo, _patterns(rule, "sources"))
        if not outputs or not sources:
            raise ReleaseError(f"generated.freshness[{index}] has no matching files or sources")
        newest_source = max(path.stat().st_mtime_ns for path in sources.values())
        stale = [name for name, path in outputs.items() if path.stat().st_mtime_ns < newest_source]
        if stale:
            raise ReleaseError("Generated files older than sources: " + ", ".join(sorted(stale)))


def verify_live(repo: Path, entries: Any) -> None:
    if not isinstance(entries, list) or not entries or not all(isinstance(item, dict) for item in entries):
        raise ConfigError("live_artifacts must be a nonempty array of tables")
    for entry in entries:
        name, url = entry.get("file"), entry.get("url")
        if not isinstance(name, str) or not name or Path(name).is_absolute() or \
                ".." in Path(name).parts or not isinstance(url, str) or \
                not url.startswith(("http://", "https://")):
            raise ConfigError("live_artifacts entries need a relative file and HTTP(S) url")
        path = repo / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(repo.resolve()):
            raise ReleaseError(f"Live artifact local file is missing or unsafe: {name}")
        try:
            request = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(request, timeout=15) as response:
                length = response.headers.get("Content-Length")
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise ReleaseError(f"Cannot verify live artifact {url}: {exc}") from exc
        if length is None or not length.isdecimal():
            raise ReleaseError(f"Live artifact has no Content-Length: {url}")
        if int(length) != path.stat().st_size:
            raise ReleaseError(f"Live artifact size differs: {url} ({length} live, {path.stat().st_size} local)")
        print(f"Live artifact matches: {url} ({length} bytes)")
