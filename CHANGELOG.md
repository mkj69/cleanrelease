# Changelog

## 0.3.0

Add required executable and PATH options to the exported-tree test gate, and report pytest skips suggesting missing tools. Gate generated files against the selected release ref and source modification times. Add an explicit post-publish `verify-live` command to compare HTTP Content-Length with local artifacts.

## 0.2.0

Add an optional exported tree test gate with a declared dependency import guard. Add optional JSON key stripping, disabled by default to avoid diverging from generated source data. Document how to check rendered site language and paths with sitecheck before release.

## 0.1.0

Initial release with configurable content checks, public and archive Git workflows, optional site and GitHub steps, and fresh install checks.
