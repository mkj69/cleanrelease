---
name: cleanrelease
description: Check a selected Git release ref before publication, including exported-tree tests and generated artifacts, then verify deployed files.
---

# Clean release gates

Before merging into a release branch, regenerate media and data exports and commit them. Configure `[generated].paths` for all generated outputs; `cleanrelease check` compares those files (including additions and deletions) in `[source].ref` to the working tree. For source freshness, add `[[generated.freshness]]` rules with `files` and `sources`; working-tree mtimes must be meaningful, so rebuild after a fresh checkout when needed. A green test run is not enough if generated outputs were never committed.

When exported-tree tests call tools such as Node or a browser, set `[test_public_tree].require_tools` to their executable names. Prepend custom tool directories via `path` (relative to the source repo or absolute). The gate fails if a required tool is unavailable and reports pytest skips with missing-tool reasons. Investigate any reported skip even when tests pass. Then run `cleanrelease publish --dry-run` before publishing.

After publishing and waiting for deployment, configure `[[live_artifacts]]` with `file` and `url` pairs and run `cleanrelease verify-live`. HEAD Content-Length must match local file size; a matching size alone does not prove identical content. See `README.md` for configuration details. Do not treat a local build as evidence that the live site serves new files.
