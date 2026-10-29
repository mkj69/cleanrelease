"""Optional transformations and tests for the exported tree."""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement

from .config import ConfigError, matches, strings
from .gitops import ReleaseError


def strip_json(root: Path, settings: dict[str, Any]) -> int:
    if not settings.get("enabled", False):
        return 0
    paths = strings(settings, "paths")
    if not paths:
        raise ConfigError("public.strip_json.paths must contain at least one pattern")
    pattern = settings.get("key_pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ConfigError("public.strip_json.key_pattern must be a nonempty regex")
    try:
        key_pattern = re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"Invalid public.strip_json.key_pattern: {exc}") from exc

    def remove(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: remove(item) for key, item in value.items()
                    if not key_pattern.search(key)}
        if isinstance(value, list):
            return [remove(item) for item in value]
        return value

    changed = 0
    for path in sorted(root.rglob("*.json")):
        if path.is_symlink() or not matches(path.relative_to(root).as_posix(), paths):
            continue
        source = path.read_text(encoding="utf-8")
        try:
            original = json.loads(source)
        except json.JSONDecodeError as exc:
            raise ReleaseError(f"Cannot strip JSON in {path.relative_to(root)}: {exc}") from exc
        stripped = remove(original)
        if stripped != original:
            path.write_text(json.dumps(stripped, ensure_ascii=False, indent=2) +
                            ("\n" if source.endswith("\n") else ""), encoding="utf-8")
            changed += 1
    return changed


def _allowed_imports(root: Path, extra: list[str]) -> set[str]:
    manifest = root / "pyproject.toml"
    if not manifest.is_file():
        raise ConfigError("test_public_tree requires pyproject.toml in the exported tree")
    try:
        if sys.version_info >= (3, 11):
            import tomllib
        else:
            import tomli as tomllib
        with manifest.open("rb") as stream:
            project = tomllib.load(stream).get("project", {})
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read exported pyproject.toml: {exc}") from exc
    dependencies = project.get("dependencies", [])
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise ConfigError("project.optional-dependencies must be a table")
    dev = optional.get("dev", [])
    if not all(isinstance(group, list) and all(isinstance(item, str) for item in group)
               for group in (dependencies, dev)):
        raise ConfigError("project dependencies and dev dependencies must be lists of strings")
    allowed = set(extra)
    if any(not name.isidentifier() for name in allowed):
        raise ConfigError("test_public_tree.allow entries must be import root names")
    names = [project.get("name", ""), *dependencies, *dev]
    try:
        queue = [Requirement(name) for name in names if name]
    except (InvalidRequirement, TypeError) as exc:
        raise ConfigError(f"Invalid project dependency: {exc}") from exc
    distributions: set[str] = set()
    while queue:
        requirement = queue.pop()
        if requirement.marker and not any(requirement.marker.evaluate({"extra": selected})
                                          for selected in ("", *requirement.extras)):
            continue
        name = re.sub(r"[-_.]+", "-", requirement.name).lower()
        if name in distributions:
            continue
        distributions.add(name)
        allowed.add(requirement.name.replace("-", "_").replace(".", "_"))
        try:
            installed = importlib.metadata.distribution(requirement.name)
        except importlib.metadata.PackageNotFoundError:
            continue
        try:
            queue.extend(Requirement(item) for item in (installed.requires or []))
        except InvalidRequirement as exc:
            raise ConfigError(f"Invalid installed dependency: {exc}") from exc
    for module, owners in importlib.metadata.packages_distributions().items():
        if any(re.sub(r"[-_.]+", "-", owner).lower() in distributions for owner in owners):
            allowed.add(module)
    return allowed


def test_public_tree(root: Path, settings: dict[str, Any], temp: Path, repo: Path | None = None) -> None:
    if not settings.get("enabled", False):
        return
    command = settings.get("command", [sys.executable, "-m", "pytest"])
    if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
        raise ConfigError("test_public_tree.command must be a nonempty argument list")
    allowed = _allowed_imports(root, strings(settings, "allow"))
    blocker = temp / "dependency-blocker"
    blocker.mkdir()
    (blocker / "sitecustomize.py").write_text(
        Path(__file__).with_name("sitecustomize.py").read_text(encoding="utf-8"), encoding="utf-8")
    env = os.environ.copy()
    path_entries = strings(settings, "path")
    if any(not entry for entry in path_entries):
        raise ConfigError("test_public_tree.path entries must not be empty")
    env["PATH"] = os.pathsep.join([*(str((repo or root) / entry) if not Path(entry).is_absolute()
                                     else entry for entry in path_entries), env.get("PATH", "")])
    required = strings(settings, "require_tools")
    if any(not tool or Path(tool).name != tool for tool in required):
        raise ConfigError("test_public_tree.require_tools must contain executable names")
    missing = [tool for tool in required if shutil.which(tool, path=env["PATH"]) is None]
    if missing:
        raise ReleaseError("Public tree tests require missing tools: " + ", ".join(missing))
    env["PYTEST_ADDOPTS"] = (env.get("PYTEST_ADDOPTS", "") + " -rs").strip()
    env["PYTHONPATH"] = os.pathsep.join(str(path) for path in (blocker, root / "src") if path.is_dir())
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["CLEANRELEASE_ALLOWED_IMPORTS"] = json.dumps(sorted(allowed))
    try:
        result = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, check=False)
    except OSError as exc:
        raise ReleaseError(f"Public tree test command failed to start: {exc}") from exc
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        if re.match(r"^SKIPPED \[\d+\] ", line) and re.search(
                r"\b(missing|not found|not installed|not available|unavailable|not on PATH)\b",
                line, re.IGNORECASE):
            print(f"Possible missing-tool skip: {line}", file=sys.stderr)
    if result.returncode:
        detail = (result.stdout + "\n" + result.stderr).strip()[-3000:]
        raise ReleaseError(f"Public tree tests failed (exit {result.returncode}):\n{detail}")
    print("Public tree tests passed")
