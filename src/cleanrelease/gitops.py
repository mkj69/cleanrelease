"""Local Git staging and remote release operations."""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

from .config import ConfigError, matches, strings


class ReleaseError(RuntimeError):
    """A release operation could not complete."""


def git(repo: Path, *args: str, input: bytes | None = None,
        env: dict[str, str] | None = None) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], input=input,
                            capture_output=True, env=env, check=False)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ReleaseError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def resolve_ref(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()


def _safe_rel(name: str) -> Path:
    path = PurePosixPath(name)
    if not name or not path.parts or path.is_absolute() or ".." in path.parts or ".git" in path.parts:
        raise ReleaseError(f"Unsafe archive path: {name}")
    return Path(*path.parts)


def export_tree(repo: Path, commit: str, target: Path, exclude: list[str]) -> None:
    target.mkdir(parents=True, exist_ok=True)
    archive = git(repo, "archive", "--format=tar", commit)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        for member in bundle:
            name = member.name.rstrip("/")
            if not name or matches(name, exclude):
                continue
            rel = _safe_rel(name)
            destination = target / rel
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as stream, destination.open("wb") as out:
                    shutil.copyfileobj(stream, out)
                destination.chmod(member.mode & 0o777)
            elif member.issym():
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(member.linkname)
            else:
                raise ReleaseError(f"Unsupported Git archive entry: {name}")


def identity(config: dict[str, Any]) -> dict[str, str]:
    name = str(config.get("author_name", "Release Bot"))
    email = str(config.get("author_email", "mkj69@users.noreply.github.com"))
    return {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}


def orphan_commit(tree: Path, branch: str, config: dict[str, Any], message: str) -> str:
    git(tree, "init", "-q")
    git(tree, "add", "-A")
    tree_id = git(tree, "write-tree").decode().strip()
    commit = git(tree, "commit-tree", tree_id, input=(message + "\n").encode(),
                 env=identity(config)).decode().strip()
    git(tree, "update-ref", f"refs/heads/{branch}", commit)
    return commit


_PERSON = re.compile(rb"^(.*?) <([^<>]*)> ([0-9]+ [+-][0-9]{4})$")


def _commit_env(raw: bytes) -> tuple[dict[str, str], bytes]:
    head, message = raw.split(b"\n\n", 1)
    result = os.environ.copy()
    for role in ("author", "committer"):
        line = next((line[len(role) + 1:] for line in head.splitlines()
                     if line.startswith(role.encode() + b" ")), None)
        match = _PERSON.match(line or b"")
        if not match:
            raise ReleaseError(f"Cannot parse {role} in source commit")
        prefix = "GIT_" + role.upper()
        result[prefix + "_NAME"] = match[1].decode("utf-8", "surrogateescape")
        result[prefix + "_EMAIL"] = match[2].decode("utf-8", "surrogateescape")
        result[prefix + "_DATE"] = match[3].decode()
    return result, message


def keep_history(repo: Path, source_commit: str, stage: Path,
                 exclude: list[str]) -> str:
    """Rebuild reachable commits with selected paths removed from every tree."""
    git(stage, "init", "-q")
    git(stage, "fetch", "-q", "--no-tags", str(repo), source_commit)
    history = git(stage, "rev-list", "--reverse", "--topo-order", "--parents",
                  source_commit).decode().splitlines()
    mapped: dict[str, str] = {}
    index = stage / ".git" / "release-index"
    env_index = {**os.environ, "GIT_INDEX_FILE": str(index)}
    for record in history:
        old, *parents = record.split()
        git(stage, "read-tree", "--empty", env=env_index)
        entries = git(stage, "ls-tree", "-r", "-z", old).split(b"\0")
        selected = []
        for entry in entries:
            if not entry:
                continue
            metadata, path = entry.split(b"\t", 1)
            rel = path.decode("utf-8", "surrogateescape")
            if not matches(rel, exclude):
                selected.append(metadata + b"\t" + path + b"\0")
        if selected:
            git(stage, "update-index", "-z", "--index-info", input=b"".join(selected),
                env=env_index)
        tree_id = git(stage, "write-tree", env=env_index).decode().strip()
        commit_env, message = _commit_env(git(stage, "cat-file", "-p", old))
        args = ["commit-tree", tree_id]
        for parent in parents:
            args.extend(["-p", mapped[parent]])
        mapped[old] = git(stage, *args, input=message, env=commit_env).decode().strip()
    return mapped[source_commit]


def resolve_remote(repo: Path, remote: str) -> str:
    if not remote or "://" in remote or remote.startswith("git@") or Path(remote).is_absolute():
        return remote
    if remote in git(repo, "remote").decode().splitlines():
        return remote
    return str((repo / remote).resolve())


def push(repo: Path, remote: str, commit: str, branch: str, force: bool) -> None:
    if not remote:
        raise ConfigError("A remote is required to push")
    args = ["push"]
    if force:
        args.append("--force")
    git(repo, *args, remote, f"{commit}:refs/heads/{branch}")


def _copy_snapshot_path(source: Path, target: Path, name: str, max_size: int) -> None:
    rel = _safe_rel(name.rstrip("/"))
    src, dst = source / rel, target / rel
    if not src.exists() and not src.is_symlink():
        return
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.is_dir():
        shutil.rmtree(dst)
    if src.is_symlink():
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(os.readlink(src))
    elif src.is_file():
        if src.stat().st_size > max_size:
            raise ReleaseError(f"Snapshot file exceeds size cap: {rel}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    elif src.is_dir():
        for path in src.rglob("*"):
            item = path.relative_to(source)
            _safe_rel(item.as_posix())
            out = target / item
            if path.is_symlink():
                out.parent.mkdir(parents=True, exist_ok=True)
                out.symlink_to(os.readlink(path))
            elif path.is_dir():
                out.mkdir(parents=True, exist_ok=True)
            elif path.is_file():
                if path.stat().st_size > max_size:
                    raise ReleaseError(f"Snapshot file exceeds size cap: {item}")
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, out)


def archive(repo: Path, stage: Path, config: dict[str, Any], dry_run: bool) -> list[str]:
    stage.mkdir(parents=True)
    git(stage, "init", "-q", "--bare")
    git(stage, "fetch", "-q", str(repo), "+refs/heads/*:refs/heads/*",
        "+refs/tags/*:refs/tags/*")
    excluded = strings(config, "exclude_refs")
    refs = git(stage, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags").decode().splitlines()
    for ref in refs:
        if matches(ref, excluded):
            git(stage, "update-ref", "-d", ref)
    paths = strings(config, "snapshot_paths")
    if paths:
        branch = str(config.get("snapshot_branch", "archive-snapshot"))
        base = resolve_ref(repo, str(config.get("snapshot_from", "HEAD")))
        snapshot = stage.parent / "snapshot"
        git(stage, "worktree", "add", "-q", "--detach", str(snapshot), base)
        cap = config.get("max_file_size", 50_000_000)
        if not isinstance(cap, int) or cap < 0:
            raise ConfigError("archive.max_file_size must be a nonnegative integer")
        for name in paths:
            _copy_snapshot_path(repo, snapshot, name, cap)
        for path in snapshot.rglob("*"):
            if ".git" not in path.relative_to(snapshot).parts and path.is_file() and not path.is_symlink() and path.stat().st_size > cap:
                raise ReleaseError(f"Snapshot file exceeds size cap: {path.relative_to(snapshot)}")
        git(snapshot, "add", "-A")
        git(snapshot, "commit", "-q", "--allow-empty", "-m", "Archive snapshot",
            env=identity(config))
        snapshot_commit = resolve_ref(snapshot, "HEAD")
        git(stage, "update-ref", f"refs/heads/{branch}", snapshot_commit)
    selected = git(stage, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags").decode().splitlines()
    if not dry_run:
        remote = resolve_remote(repo, str(config.get("remote", "")))
        if not remote:
            raise ConfigError("archive.remote is required")
        if selected:
            git(stage, "push", "--atomic", "--force", remote,
                *[f"{ref}:{ref}" for ref in selected])
    return selected
