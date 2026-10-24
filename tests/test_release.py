from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from cleanrelease.cli import main
from cleanrelease.config import load
from cleanrelease.gitops import resolve_remote


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            text=True, check=True)
    return result.stdout.strip()


def commit(repo: Path, message: str = "Source commit") -> None:
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=Example User", "-c",
        "user.email=user@example.invalid", "commit", "-qm", message)


def configure(repo: Path, public: Path | None = None, archive: Path | None = None,
              extra: str = "") -> None:
    public_url = json.dumps(str(public) if public else "")
    archive_url = json.dumps(str(archive) if archive else "")
    (repo / "cleanrelease.toml").write_text(f'''
[source]
ref = "HEAD"
[public]
remote = {public_url}
branch = "main"
history = "orphan"
force = false
exclude = ["cleanrelease.toml", "notes/", "data/", "outputs/"]
author_name = "Release Bot"
author_email = "release@example.invalid"
[checks]
max_file_size = 12
utf8_only = true
forbidden_paths = ["**/*_results.md"]
forbidden_regexes = ["secret-[0-9]+"]
email_allow = ["allowed.txt"]
name_patterns = ["Example Researcher"]
name_allow = ["allowed.txt"]
[[checks.scripts]]
name = "CJK"
pattern = "[\\u3400-\\u9fff]"
allow = ["allowed.txt"]
[archive]
remote = {archive_url}
exclude_refs = ["refs/heads/worktree-*", "refs/tags/temporary-*"]
snapshot_paths = ["notes/"]
snapshot_branch = "archive-snapshot"
max_file_size = 12
{extra}
''', encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    (source / "public.txt").write_text("hello\n", encoding="utf-8")
    commit(source)
    configure(source)
    return source


def bare(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    target.mkdir()
    git(target, "init", "-q", "--bare")
    return target


@pytest.mark.parametrize("name,content,rule", [
    ("bad.txt", "secret-123", "forbidden regex 1"),
    ("bad.txt", "中", "CJK"),
    ("bad.txt", "person@example.com", "email"),
    ("bad.txt", "Example Researcher", "name"),
    ("bad.txt", "a" * 13, "file size"),
    ("sample_results.md", "ok", "forbidden path"),
])
def test_each_content_gate(repo: Path, capsys: pytest.CaptureFixture[str],
                           name: str, content: str, rule: str) -> None:
    (repo / name).write_text(content, encoding="utf-8")
    commit(repo)
    assert main(["--repo", str(repo), "check"]) == 1
    report = capsys.readouterr().err
    assert f"{name}:" in report
    assert rule in report


def test_allow_lists_and_command_gate(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "allowed.txt").write_text("中 Example Researcher person@example.com\n", encoding="utf-8")
    commit(repo)
    assert main(["--repo", str(repo), "check"]) == 1  # size cap still applies
    config = repo / "cleanrelease.toml"
    config.write_text(config.read_text().replace("max_file_size = 12", "max_file_size = 1000"))
    assert main(["--repo", str(repo), "check"]) == 0
    config.write_text(config.read_text() + '\n[[checks.commands]]\nname = "docs"\nargv = ["python3", "-c", "import sys; sys.exit(3)"]\n')
    assert main(["--repo", str(repo), "check"]) == 1
    assert "docs:" in capsys.readouterr().err


def test_symlink_gate(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "link").symlink_to("public.txt")
    commit(repo)
    assert main(["--repo", str(repo), "check"]) == 1
    assert "symlink" in capsys.readouterr().err


def test_charset_gate(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "bad.txt").write_bytes(b"\xff")
    commit(repo)
    assert main(["--repo", str(repo), "check"]) == 1
    assert "bad.txt: charset" in capsys.readouterr().err


def test_public_orphan_exclusions_force_and_dry_run(repo: Path, tmp_path: Path) -> None:
    remote = bare(tmp_path, "public.git")
    configure(repo, public=remote)
    for folder in ("notes", "data", "outputs"):
        (repo / folder).mkdir()
        (repo / folder / "hidden.txt").write_text("private\n")
    commit(repo)
    before = git(repo, "status", "--porcelain")
    assert main(["--repo", str(repo), "publish", "--dry-run"]) == 0
    assert git(repo, "status", "--porcelain") == before
    assert not git(remote, "for-each-ref", "--format=%(refname)")
    assert main(["--repo", str(repo), "publish"]) == 0
    names = git(remote, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert names == ["public.txt"]
    assert len(git(remote, "rev-list", "--parents", "main").split()) == 1
    first = git(remote, "rev-parse", "main")
    (repo / "public.txt").write_text("later\n")
    commit(repo)
    assert main(["--repo", str(repo), "publish"]) == 1
    assert git(remote, "rev-parse", "main") == first
    assert main(["--repo", str(repo), "publish", "--force"]) == 0
    assert git(remote, "show", "main:public.txt") == "later"


def test_relative_remotes_publish_and_archive(repo: Path, tmp_path: Path) -> None:
    public = bare(tmp_path, "public.git")
    archive = bare(tmp_path, "archive.git")
    configure(repo, public=Path("../public.git"), archive=Path("../archive.git"))

    assert main(["--repo", str(repo), "publish"]) == 0
    assert git(public, "show", "main:public.txt") == "hello"
    assert len(git(public, "rev-list", "--parents", "main").split()) == 1

    assert main(["--repo", str(repo), "archive"]) == 0
    assert git(archive, "rev-parse", "main") == git(repo, "rev-parse", "HEAD")
    assert "refs/heads/archive-snapshot" in git(
        archive, "for-each-ref", "--format=%(refname)").splitlines()


def test_remote_resolution_preserves_urls_absolute_paths_and_names(repo: Path,
                                                                   tmp_path: Path) -> None:
    git(repo, "remote", "add", "origin", str(tmp_path / "public.git"))
    for remote in ("origin", "https://example.invalid/public.git",
                   "ssh://example.invalid/public.git", "git@example.invalid:public.git",
                   str(tmp_path / "public.git")):
        assert resolve_remote(repo, remote) == remote
    assert resolve_remote(repo, "../public.git") == str(tmp_path / "public.git")


def test_keep_history_rewrites_excluded_paths(repo: Path, tmp_path: Path) -> None:
    remote = bare(tmp_path, "public.git")
    (repo / "notes").mkdir()
    (repo / "notes" / "draft.txt").write_text("draft")
    commit(repo)
    configure(repo, public=remote)
    config = repo / "cleanrelease.toml"
    config.write_text(config.read_text().replace('history = "orphan"', 'history = "keep"'))
    assert main(["--repo", str(repo), "publish"]) == 0
    assert len(git(remote, "rev-list", "main").splitlines()) == 2
    for revision in git(remote, "rev-list", "main").splitlines():
        assert "notes/draft.txt" not in git(remote, "ls-tree", "-r", "--name-only", revision)


def test_pages_builds_orphan_branch(repo: Path, tmp_path: Path) -> None:
    remote = bare(tmp_path, "public.git")
    configure(repo, public=remote, extra='''
[pages]
enabled = true
build = ["python3", "-c", "from pathlib import Path; Path('site').mkdir(); Path('site/index.html').write_text('site')"]
output = "site"
branch = "gh-pages"
''')
    assert main(["--repo", str(repo), "publish"]) == 0
    assert git(remote, "show", "gh-pages:index.html") == "site"
    assert ".nojekyll" in git(remote, "ls-tree", "-r", "--name-only", "gh-pages")
    assert len(git(remote, "rev-list", "--parents", "gh-pages").split()) == 1
    assert "site/index.html" not in git(remote, "ls-tree", "-r", "--name-only", "main")


def test_archive_refs_snapshot_and_dry_run(repo: Path, tmp_path: Path) -> None:
    remote = bare(tmp_path, "archive.git")
    configure(repo, archive=remote)
    git(repo, "branch", "feature")
    git(repo, "branch", "worktree-test")
    git(repo, "tag", "stable")
    git(repo, "tag", "temporary-test")
    (repo / "notes").mkdir()
    (repo / "notes" / "memo.txt").write_text("memo")
    assert main(["--repo", str(repo), "archive", "--dry-run"]) == 0
    assert not git(remote, "for-each-ref", "--format=%(refname)")
    assert main(["--repo", str(repo), "archive"]) == 0
    refs = git(remote, "for-each-ref", "--format=%(refname)").splitlines()
    assert refs == ["refs/heads/archive-snapshot", "refs/heads/feature", "refs/heads/main", "refs/tags/stable"]
    assert git(remote, "show", "archive-snapshot:notes/memo.txt") == "memo"
    assert git(remote, "rev-parse", "archive-snapshot^1") == git(repo, "rev-parse", "HEAD")
    assert main(["--repo", str(repo), "archive"]) == 0


def test_archive_size_cap(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    configure(repo, archive=bare(tmp_path, "archive.git"))
    (repo / "notes").mkdir()
    (repo / "notes" / "large.txt").write_text("x" * 13)
    assert main(["--repo", str(repo), "archive", "--dry-run"]) == 1
    assert "size cap" in capsys.readouterr().err


def test_mock_gh_and_missing_gh(repo: Path, tmp_path: Path,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    remote = bare(tmp_path, "public.git")
    configure(repo, public=remote, extra='''
[github]
repo = "sample/public"
visibility = "public"
discussions = true
homepage = "https://example.invalid/"
topics = ["research"]
''')
    script_dir = tmp_path / "bin"
    script_dir.mkdir()
    log = tmp_path / "gh.log"
    gh = script_dir / "gh"
    gh.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$MOCK_GH_LOG"\n')
    gh.chmod(0o755)
    monkeypatch.setenv("MOCK_GH_LOG", str(log))
    monkeypatch.setenv("PATH", str(script_dir) + os.pathsep + os.environ["PATH"])
    assert main(["--repo", str(repo), "publish", "--no-public"]) == 0
    calls = log.read_text()
    assert "--enable-discussions" in calls
    assert "--homepage" in calls
    assert "--add-topic" in calls
    assert "--visibility" not in calls


def test_missing_gh_skips_settings(repo: Path, tmp_path: Path,
                                   monkeypatch: pytest.MonkeyPatch,
                                   capsys: pytest.CaptureFixture[str]) -> None:
    remote = bare(tmp_path, "public.git")
    configure(repo, public=remote, extra='''
[github]
repo = "sample/public"
discussions = true
''')
    monkeypatch.setattr("cleanrelease.cli.shutil.which", lambda name: None)
    assert main(["--repo", str(repo), "publish"]) == 0
    assert "gh unavailable" in capsys.readouterr().out


def test_regex_rule_allow_list(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "allowed.txt").write_text("marker-77")
    (repo / "bad.txt").write_text("marker-77")
    commit(repo)
    config = repo / "cleanrelease.toml"
    config.write_text(config.read_text() + '''
[[checks.regex]]
name = "marker"
pattern = "marker-[0-9]+"
allow = ["allowed.txt"]
''')
    assert main(["--repo", str(repo), "check"]) == 1
    report = capsys.readouterr().err
    assert "bad.txt:1: marker" in report
    assert "allowed.txt:1: marker" not in report


def test_example_config_parses() -> None:
    example = Path(__file__).resolve().parents[1] / "examples" / "cleanrelease.toml"
    assert load(example)["public"]["history"] == "orphan"


def test_init_writes_parseable_config(tmp_path: Path) -> None:
    assert main(["--repo", str(tmp_path), "init"]) == 0
    assert load(tmp_path / "cleanrelease.toml")["source"]["ref"] == "HEAD"
    assert main(["--repo", str(tmp_path), "init"]) == 1
