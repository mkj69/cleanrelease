"""Command line interface for cleanrelease."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .config import EXAMPLE, ConfigError, load, strings, table
from .artifacts import check_generated, verify_live
from .gates import check_tree
from .gitops import ReleaseError, archive, export_tree, git, keep_history, orphan_commit, push, resolve_ref, resolve_remote
from .public_tree import strip_json, test_public_tree


def _run(argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True,
                            check=False, env=env)
    if result.returncode:
        raise ReleaseError(f"{' '.join(argv)} failed: {(result.stderr or result.stdout).strip()}")


def _check(repo: Path, config: dict[str, Any], temp: Path) -> tuple[str, Path]:
    source = table(config, "source")
    public = table(config, "public")
    commit = resolve_ref(repo, str(source.get("ref", "HEAD")))
    check_generated(repo, commit, table(config, "generated"), temp)
    tree = temp / "check"
    export_tree(repo, commit, tree, strings(public, "exclude"))
    strip_settings = public.get("strip_json", {})
    if not isinstance(strip_settings, dict):
        raise ConfigError("[public.strip_json] must be a table")
    if strip_settings.get("enabled", False) and public.get("history", "orphan") == "keep":
        raise ConfigError("public.strip_json is not supported with keep history")
    stripped = strip_json(tree, strip_settings)
    if strip_settings.get("enabled", False):
        print(f"WARNING: JSON field stripping changed {stripped} files and may desync generated data from its generator", file=sys.stderr)
    violations = check_tree(tree, table(config, "checks"))
    if violations:
        for item in violations:
            print(item, file=sys.stderr)
        raise ReleaseError(f"Check failed: {len(violations)} violation(s)")
    test_settings = table(config, "test_public_tree")
    if test_settings.get("enabled", False):
        test_tree = temp / "test-public"
        shutil.copytree(tree, test_tree, symlinks=True)
        test_public_tree(test_tree, test_settings, temp, repo)
    print(f"Check passed: {len([p for p in tree.rglob('*') if p.is_file()])} files")
    return commit, tree


def _github(config: dict[str, Any], dry_run: bool, no_public: bool) -> None:
    settings = table(config, "github")
    repo = str(settings.get("repo", ""))
    if not repo:
        return
    steps: list[list[str]] = []
    visibility = str(settings.get("visibility", ""))
    if visibility and not no_public:
        if visibility not in ("public", "private"):
            raise ConfigError("github.visibility must be public or private")
        steps.append(["repo", "edit", repo, "--visibility", visibility,
                      "--accept-visibility-change-consequences"])
    if settings.get("discussions", False):
        steps.append(["repo", "edit", repo, "--enable-discussions"])
    homepage = str(settings.get("homepage", ""))
    if homepage:
        steps.append(["repo", "edit", repo, "--homepage", homepage])
    topics = strings(settings, "topics")
    if topics:
        steps.append(["repo", "edit", repo, "--add-topic", ",".join(topics)])
    if settings.get("pages", False):
        branch = str(table(config, "pages").get("branch", "gh-pages"))
        steps.append(["api", "-X", "PUT", f"repos/{repo}/pages",
                      "-f", f"source[branch]={branch}", "-f", "source[path]=/"])
    categories = settings.get("discussion_categories", [])
    if not isinstance(categories, list):
        raise ConfigError("github.discussion_categories must be a list")
    if dry_run:
        for step in steps:
            print("DRY RUN: gh " + " ".join(step))
        if categories:
            print("DRY RUN: gh verify discussion categories")
        return
    executable = shutil.which("gh")
    if not executable:
        print("gh unavailable: skipping GitHub settings")
        return
    for step in steps:
        if step[:3] == ["api", "-X", "PUT"]:
            probe = subprocess.run([executable, "api", step[3]], capture_output=True)
            if probe.returncode:
                step[2] = "POST"
        _run([executable, *step], Path.cwd())
    if categories:
        owner, separator, name = repo.partition("/")
        if not separator or not name:
            raise ConfigError("github.repo must be owner/repo")
        query = ("query($owner:String!,$name:String!){repository(owner:$owner,name:$name)"
                 "{discussionCategories(first:100){nodes{name}}}}")
        result = subprocess.run([executable, "api", "graphql", "-f", f"query={query}",
                                 "-f", f"owner={owner}", "-f", f"name={name}"],
                                capture_output=True, text=True, check=False)
        if result.returncode:
            raise ReleaseError(f"gh repository lookup failed: {result.stderr.strip()}")
        try:
            existing = {item["name"] for item in
                        json.loads(result.stdout)["data"]["repository"]["discussionCategories"]["nodes"]}
        except (KeyError, TypeError, ValueError) as exc:
            raise ReleaseError("gh returned no discussion categories") from exc
        for category in categories:
            if not isinstance(category, dict) or not category.get("name"):
                raise ConfigError("Each discussion category needs a name")
            name = str(category["name"])
            if name in existing:
                print(f"Discussion category present: {name}")
            else:
                print(f"Discussion category missing: {name}; create it in repository settings")


def _publish(repo: Path, config: dict[str, Any], dry_run: bool,
             force_flag: bool, no_public: bool, temp: Path) -> None:
    commit, checked_tree = _check(repo, config, temp)
    settings = table(config, "public")
    branch = str(settings.get("branch", "main"))
    history = str(settings.get("history", "orphan"))
    exclude = strings(settings, "exclude")
    force = force_flag or bool(settings.get("force", False))
    stage = temp / "public"
    stage.mkdir()
    if history == "orphan":
        shutil.copytree(checked_tree, stage, dirs_exist_ok=True, symlinks=True)
        public_commit = orphan_commit(stage, branch, settings,
                                      str(settings.get("message", "Public release")))
    elif history == "keep":
        public_commit = keep_history(repo, commit, stage, exclude)
    else:
        raise ConfigError("public.history must be orphan or keep")
    remote = resolve_remote(repo, str(settings.get("remote", "")))
    print(f"Public {branch}: {public_commit} ({history} history)")
    pages = table(config, "pages")
    pages_commit = None
    pages_stage = temp / "pages"
    if pages.get("enabled", False):
        build = strings(pages, "build")
        if not build:
            raise ConfigError("pages.build must contain a command when pages is enabled")
        build_tree = temp / "build"
        shutil.copytree(checked_tree, build_tree, symlinks=True)
        _run(build, build_tree)
        output = Path(str(pages.get("output", "site")))
        if output.is_absolute() or ".." in output.parts:
            raise ConfigError("pages.output must be a relative directory")
        built = build_tree / output
        if not built.is_dir():
            raise ReleaseError(f"Pages output directory does not exist: {output}")
        shutil.copytree(built, pages_stage, symlinks=True)
        (pages_stage / ".nojekyll").touch()
        pages_branch = str(pages.get("branch", "gh-pages"))
        pages_commit = orphan_commit(pages_stage, pages_branch, settings,
                                     str(pages.get("message", "Publish site")))
        print(f"Pages {pages_branch}: {pages_commit}")
    if dry_run:
        print(f"DRY RUN: push {branch} to {remote or '<unset>'}, force={force}")
        if pages_commit:
            print(f"DRY RUN: push {pages.get('branch', 'gh-pages')} to {remote or '<unset>'}, force={force}")
    else:
        push(stage, remote, public_commit, branch, force)
        if pages_commit:
            push(pages_stage, remote, pages_commit, str(pages.get("branch", "gh-pages")), force)
    _github(config, dry_run, no_public)


def _install_check(repo: Path, config: dict[str, Any], temp: Path) -> None:
    settings = table(config, "install_check")
    dist = temp / "dist"
    isolated_env = os.environ.copy()
    isolated_env.pop("PYTHONPATH", None)
    isolated_env["PYTHONNOUSERSITE"] = "1"
    build = subprocess.run([sys.executable, "-m", "build", "--outdir", str(dist), str(repo)],
                           text=True, capture_output=True, check=False, env=isolated_env)
    if build.returncode:
        raise ReleaseError(f"Build failed: {(build.stderr or build.stdout).strip()[-600:]}")
    wheels = list(dist.glob("*.whl"))
    sdists = list(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseError("Expected one wheel and one source distribution")
    smoke = settings.get("smoke", [["python", "-c", "import cleanrelease"],
                                   ["cleanrelease", "--help"]])
    if not isinstance(smoke, list) or not all(isinstance(argv, list) and argv and
                                              all(isinstance(item, str) for item in argv)
                                              for argv in smoke):
        raise ConfigError("install_check.smoke must be a list of argument lists")
    for kind, artifact in (("wheel", wheels[0]), ("sdist", sdists[0])):
        venv = temp / f"{kind}-venv"
        _run([sys.executable, "-m", "venv", str(venv)], temp)
        bin_dir = venv / ("Scripts" if sys.platform == "win32" else "bin")
        pip = bin_dir / ("pip.exe" if sys.platform == "win32" else "pip")
        run_env = {**isolated_env, "VIRTUAL_ENV": str(venv),
                   "PATH": str(bin_dir) + os.pathsep + isolated_env.get("PATH", "")}
        _run([str(pip), "install", str(artifact)], temp, run_env)
        _run([str(pip), "check"], temp, run_env)
        run_dir = temp / f"{kind}-run"
        run_dir.mkdir()
        for argv in smoke:
            first = bin_dir / argv[0]
            if not first.is_file():
                raise ReleaseError(f"Smoke executable is not installed in {kind} environment: {argv[0]}")
            _run([str(first), *argv[1:]], run_dir, run_env)
        print(f"{kind} install and smoke checks passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cleanrelease")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="source Git repository")
    parser.add_argument("--config", type=Path, default=Path("cleanrelease.toml"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="write a commented example config")
    commands.add_parser("check", help="check the would-be public tree")
    archive_parser = commands.add_parser("archive", help="push selected history")
    archive_parser.add_argument("--dry-run", action="store_true")
    publish_parser = commands.add_parser("publish", help="publish selected content")
    publish_parser.add_argument("--dry-run", action="store_true")
    publish_parser.add_argument("--force", action="store_true")
    publish_parser.add_argument("--no-public", action="store_true")
    commands.add_parser("install-check", help="test fresh installs of both distributions")
    commands.add_parser("verify-live", help="compare published artifact sizes with local files")
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    config_path = args.config if args.config.is_absolute() else repo / args.config
    try:
        if args.command == "init":
            if config_path.exists():
                raise ConfigError(f"Config already exists: {config_path}")
            config_path.write_text(EXAMPLE, encoding="utf-8")
            print(f"Wrote {config_path}")
            return 0
        config = load(config_path)
        with tempfile.TemporaryDirectory(prefix="cleanrelease-") as scratch:
            temp = Path(scratch)
            if args.command == "check":
                _check(repo, config, temp)
            elif args.command == "archive":
                refs = archive(repo, temp / "archive.git", table(config, "archive"), args.dry_run)
                for ref in refs:
                    print(ref)
                if args.dry_run:
                    print("DRY RUN: archive remote unchanged")
            elif args.command == "publish":
                _publish(repo, config, args.dry_run, args.force, args.no_public, temp)
            elif args.command == "install-check":
                _install_check(repo, config, temp)
            elif args.command == "verify-live":
                verify_live(repo, config.get("live_artifacts"))
        return 0
    except (ConfigError, ReleaseError, OSError) as exc:
        print(f"cleanrelease: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
