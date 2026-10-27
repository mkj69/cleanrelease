# cleanrelease

cleanrelease checks a selected Git tree before it is published. It can also copy full history to an archive remote, build a site branch, and test fresh installs of a package. A public release can contain one new root commit or rewritten branch history with excluded files removed.

Originally built for the Opulse release.

The tool exists to make release rules visible in a TOML file and repeatable from a terminal or CI job. Source files are read from a committed Git ref. Local edits enter only an explicitly configured archive snapshot.

## Install

Python 3.10 or newer is required.

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Quick start

```sh
cleanrelease init
# Edit cleanrelease.toml, including the remote destinations and checks.
cleanrelease check
cleanrelease publish --dry-run
cleanrelease archive --dry-run
cleanrelease publish --force
```

Use `--repo PATH` and `--config PATH` before the subcommand to select another repository or config file. `publish --no-public` skips the visibility change. A dry run runs checks and local builds but leaves remote repositories unchanged.

## Configuration

The [example config](examples/cleanrelease.toml) shows release checks for this repository. Set the public and archive remotes before publishing. `init` writes a commented starting config.

| Table | Main keys | Effect |
| --- | --- | --- |
| `source` | `ref` | Local commit to release, default `HEAD`. |
| `public` | `remote`, `branch`, `history`, `force`, `exclude`, `author_name`, `author_email`, `message` | Selects public content and push behavior. `history` is `orphan` or `keep`. Exclusions are path globs or directory prefixes. |
| `public.strip_json` | `enabled`, `paths`, `key_pattern` | Optionally remove matching JSON keys recursively from selected files. Disabled by default and available only with orphan history. |
| `checks` | `max_file_size`, `utf8_only`, `charset_allow`, `forbidden_paths`, `forbidden_regexes`, `email_allow`, `name_patterns`, `name_allow` | Checks every exported file. Binary files get path and size checks. Symlinks fail. |
| `checks.regex` | `name`, `pattern`, `allow` | Repeated TOML tables for regular expressions with per rule file allow lists. |
| `checks.scripts` | `name`, `pattern`, `allow` | Repeated tables for script or charset patterns such as CJK. |
| `checks.commands` | `name`, `argv` | Repeated commands run in the exported tree. A nonzero exit fails the check. |
| `test_public_tree` | `enabled`, `command`, `allow` | Optionally run tests on a copy of the exported tree with undeclared installed imports blocked. |
| `archive` | `remote`, `exclude_refs`, `snapshot_paths`, `snapshot_branch`, `snapshot_from`, `max_file_size` | Pushes selected local branches and tags. Optional snapshot copies current files from named paths. |
| `pages` | `enabled`, `build`, `output`, `branch`, `message` | Builds a site from the exported tree and publishes an orphan site branch. |
| `github` | `repo`, `visibility`, `discussions`, `pages`, `homepage`, `topics`, `discussion_categories` | Optional settings applied with `gh` after Git pushes. Category names are verified. A missing `gh` skips these settings. |
| `install_check` | `smoke` | Commands run after installing each built distribution in its own fresh virtual environment. |

Patterns match paths relative to the exported tree. `forbidden_paths` reports matching files. `exclude` removes matching files before checks and publication. A rule's `allow` list suppresses that rule only for matching files. Diagnostic lines contain a path, line number when available, rule name, and a short snippet.

### Test the public tree

Enable the exported tree test gate when the public repository runs tests in CI:

```toml
[test_public_tree]
enabled = true
command = ["python", "-m", "pytest", "-q"]
allow = []
```

The gate runs during `check` and before any `publish` push, including dry runs. It tests an isolated copy of the selected committed tree after exclusions and optional JSON stripping. Private files removed from the export are unavailable to tests. Tests that require private files should skip when those files are absent. The import guard blocks installed packages outside the core and `dev` dependencies declared in the exported `pyproject.toml`, including available transitive dependencies. Standard library and exported local modules remain accessible. Add import root names to `allow` only when the release CI environment intentionally provides them. This guard is a local simulation, not a substitute for an actual clean CI installation. It does not download dependencies, block arbitrary filesystem access, or enforce dependency version constraints. Use a Python environment with pytest and the project's declared dependencies installed.

### Optional JSON field stripping

```toml
[public.strip_json]
enabled = true
paths = ["docs/assets/data/**"]
key_pattern = "(^private_|_zh$)"
```

The pattern searches keys in nested JSON objects and arrays. Only matching files that actually change are rewritten. This is off by default because editing generated data in a release can desync the output from its generator. Prefer changing the generator and rebuilding before release. When enabled, `check` prints a warning and tests the transformed export. Stripping is not supported with `history = "keep"` because older commits would retain the original data.

### Check the rendered site

cleanrelease checks source files and can build a site branch, but rendered site language, links, and published paths need a separate check. Chain [sitecheck](https://github.com/mkj69/sitecheck) before pushing:

```sh
cleanrelease check
cleanrelease publish --dry-run
# In a separate copy of the selected public export, with exclusions applied:
mkdocs build --strict -d site
sitecheck pages site
sitecheck --config sitecheck.toml lang site
sitecheck smoke site
# Back in the source repository, after reviewing the checks:
cleanrelease publish --force
```

Configure `lang_rules` in `sitecheck.toml` for the intended page paths and scripts, for example `lang_rules = [{ page = "*/en/*", scripts = ["cjk"] }]`. Review the `pages` output for unwanted paths and use `smoke` for broken links. Build `site` from the selected public export, not from private source files. Do not publish if the rendered site checks fail.

## Limits

`check` scans the selected public tree, not earlier commits. `history = "keep"` rewrites each reachable commit to remove excluded paths, but does not inspect older content for regex violations or retain commit signatures. An archive preserves selected Git history, including content that may have existed in earlier commits. Archive pushes replace selected destination refs and do not delete destination refs that are absent locally.

Remote pushes and GitHub settings are separate operations. A failure after a push can leave a partial release. `install-check` can need package dependencies and build tools from an index or local cache.

The [documented Discussions API](https://docs.github.com/en/graphql/reference/discussions) has no category creation mutation. Configure category names to check for them, then create missing categories in repository settings.
